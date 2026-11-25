"""Prova del motore: foto e video veri (generati qui), tutti i formati, pesi, errori, annullamento."""
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from PIL import Image
from PySide6.QtCore import QCoreApplication, Qt

import engine as e
from common import NO_WINDOW

app = QCoreApplication.instance() or QCoreApplication([])


def run(job, cancel=False):
    out = {}
    if job.src.suffix in (".mp4", ".mkv"):
        job.kind = "video"
    w = e.CompressWorker([job])
    w.item_done.connect(lambda i, p, n: out.update(done=p, note=n))
    w.item_failed.connect(lambda i, m, d: out.update(error=m, detail=d))
    w.cancelled.connect(lambda: out.update(cancelled=True))
    if cancel:
        w.progress.connect(lambda *_: w.stop(), Qt.DirectConnection)
    w.run()                      # sincrono: stesso codice del thread
    return out


with tempfile.TemporaryDirectory(prefix="mc test ") as t:
    d = Path(t) / "cartella con spazi è"
    d.mkdir()
    photo, video = d / "foto.png", d / "video.mp4"
    Image.effect_noise((1600, 1200), 60).convert("RGB").save(photo)
    subprocess.run([e.find_ffmpeg(), "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=s=1280x720:r=30:d=6",
                    "-f", "lavfi", "-i", "sine=d=6", "-c:v", "libx264", "-crf", "10", "-c:a", "aac", "-shortest",
                    str(video)], check=True, creationflags=NO_WINDOW)

    for fmt, ext in e.IMAGE_FORMATS.items():
        for mb in (0, 0.2):
            r = run(e.Job(photo, d, f"p_{fmt}_{mb}", fmt, "Media", mb))
            size = r["done"].stat().st_size
            assert r["done"].suffix == ext and (not mb or size <= mb * e.MB), (fmt, mb, size, r)
            Image.open(r["done"]).verify()
            print("foto", fmt, mb, size, r["note"])

    for fmt in e.VIDEO_FORMATS:
        for mb in (0, 0.8):
            r = run(e.Job(video, d, f"v_{fmt[:3]}_{fmt[-4:-1]}_{mb}", fmt, "Media", mb))
            assert "done" in r, r
            size, info = r["done"].stat().st_size, e.probe_video(r["done"])
            assert info.audio == 1 and abs(info.duration - 6) < 0.5, info
            assert not mb or size <= mb * e.MB * 1.02, (fmt, size)
            print("video", fmt, mb, size, r["note"])

    # mai sovrascrivere
    a = run(e.Job(photo, d, "stesso", "JPEG", "Alta", 0))["done"]
    b = run(e.Job(photo, d, "stesso", "JPEG", "Alta", 0))["done"]
    assert a != b and a.exists() and b.exists()

    # errori
    broken_i, broken_v = d / "rotta.jpg", d / "rotto.mp4"
    broken_i.write_bytes(b"non sono una foto")
    broken_v.write_bytes(b"non sono un video")
    for job in (e.Job(broken_i, d, "x", "JPEG", "Media", 0), e.Job(broken_v, d, "x", "MP4 (H.264)", "Media", 0),
                e.Job(photo, d, "x", "JPEG", "Media", 0.0001), e.Job(video, d, "x", "MP4 (H.264)", "Media", 0.01),
                e.Job(photo, d / "inesistente", "x", "JPEG", "Media", 0)):
        r = run(job)
        assert "error" in r, r
        print("errore ok:", r["error"])

    # annullamento: nessun file lasciato
    for job in (e.Job(video, d, "annullato", "MP4 (H.264)", "Media", 0),
                e.Job(photo, d, "annullato", "JPEG", "Media", 0.2)):
        assert run(job, cancel=True).get("cancelled")
    assert not list(d.glob("annullato*"))

    ff = [e.find_ffmpeg(), "-y", "-loglevel", "error"]
    # due tracce audio e un sottotitolo: devono restare in tutti i contenitori
    (d / "s.srt").write_text("1\n00:00:00,000 --> 00:00:02,000\nciao\n", encoding="utf-8")
    subprocess.run(ff + ["-i", str(video), "-f", "lavfi", "-i", "sine=f=800:d=6", "-i", str(d / "s.srt"), "-map",
                         "0:v", "-map", "0:a", "-map", "1:a", "-map", "2", "-c:v", "copy", "-c:a", "aac", "-c:s",
                         "srt", str(d / "multi.mkv")], check=True, creationflags=NO_WINDOW)
    gpus = (False, True) if e.gpu_available() else (False,)
    n = 0
    for fmt in e.VIDEO_FORMATS:
        for gpu in gpus:
            for mb in (0, 1.0):
                n += 1
                r = run(e.Job(d / "multi.mkv", d, f"m{n}", fmt, "Media", mb, "video", gpu))
                assert "done" in r, r
                info = e.probe_video(r["done"])
                assert info.audio == 2 and len(info.subs) == 1, (fmt, gpu, info)
                assert not mb or r["done"].stat().st_size <= mb * e.MB * 1.03, (fmt, gpu, r["done"].stat().st_size)
                assert ("scheda video" in r["note"]) == (gpu and not fmt.startswith("WEBM")), (fmt, gpu, r["note"])
        print("multitraccia ok", fmt, "gpu provata:", len(gpus) > 1)

    # peso richiesto enorme: il video non deve diventare più grande dell'originale
    for gpu in gpus:
        r = run(e.Job(video, d, f"grande{gpu}", "MP4 (H.264)", "Alta", 300, "video", gpu))
        assert r["done"].stat().st_size <= video.stat().st_size * 1.03 and "superava" in r["note"], r
    print("nessun gonfiamento ok")

    # HDR -> SDR, anche con la scheda video
    subprocess.run(ff + ["-f", "lavfi", "-i", "testsrc2=s=640x360:d=2", "-vf",
                         "zscale=tin=bt709:min=bt709:pin=bt709:rin=full:t=smpte2084:m=bt2020nc:p=bt2020:r=tv,"
                         "format=yuv420p10le", "-c:v", "libx265", "-color_primaries", "bt2020", "-color_trc",
                         "smpte2084", "-colorspace", "bt2020nc", str(d / "hdr.mp4")], check=True,
                   creationflags=NO_WINDOW, capture_output=True)
    assert e.probe_video(d / "hdr.mp4").hdr == "smpte2084"
    for gpu in gpus:
        r = run(e.Job(d / "hdr.mp4", d, f"sdr{gpu}", "MP4 (H.264)", "Media", 0, "video", gpu))
        assert "done" in r and not e.probe_video(r["done"]).hdr and "HDR" in r["note"], r
    print("HDR ok")

    # HEIC (campione nel progetto)
    shutil.copy(Path(__file__).with_name("test_sample.heic"), d / "iphone.heic")
    assert e.inspect(d / "iphone.heic").kind == "photo"
    r = run(e.Job(d / "iphone.heic", d, "da heic", "JPEG", "Media", 0))
    assert Image.open(r["done"]).size == (400, 300), r
    print("HEIC ok")

    # animazioni: tutti i fotogrammi e i tempi restano
    frames = [Image.effect_noise((200, 150), 40 + i * 10).convert("RGB") for i in range(6)]
    frames[0].save(d / "anim.gif", save_all=True, append_images=frames[1:], duration=120, loop=0)
    assert e.inspect(d / "anim.gif").kind == "anim"
    for fmt in e.ANIM_FORMATS:
        for mb in (0, 0.05):
            r = run(e.Job(d / "anim.gif", d, f"a{fmt}{mb}", fmt, "Media", mb, "anim"))
            assert "done" in r, r
            with Image.open(r["done"]) as im:
                assert im.n_frames == 6 and im.info["loop"] == 0, (fmt, im.n_frames, im.info)
                im.load()
                assert im.info["duration"] == 120, im.info
            assert not mb or r["done"].stat().st_size <= mb * e.MB
        print("animazione ok", fmt)

    # coda: un file rotto non ferma gli altri
    seen = []
    w = e.CompressWorker([e.Job(photo, d, "q1", "JPEG", "Media", 0), e.Job(broken_i, d, "q2", "JPEG", "Media", 0),
                          e.Job(photo, d, "q3", "JPEG", "Media", 0)])
    w.item_done.connect(lambda i, *_: seen.append((i, True)))
    w.item_failed.connect(lambda i, *_: seen.append((i, False)))
    w.run()
    assert seen == [(0, True), (1, False), (2, True)], seen
    print("coda ok")

    # trasparenza e rotazione EXIF
    im = Image.new("RGBA", (40, 20), (0, 0, 0, 0))
    im.save(d / "alpha.png")
    data, _ = e.compress_image(d / "alpha.png", "JPEG", e.PRESETS["Alta"], 0)
    import io
    assert min(Image.open(io.BytesIO(data)).getpixel((0, 0))) > 250
    exif = Image.Exif()
    exif[274] = 6
    Image.new("RGB", (40, 20)).save(d / "rot.jpg", exif=exif)
    assert e.compress_image(d / "rot.jpg", "JPEG", e.PRESETS["Alta"], 0)[1] == (20, 40)

assert e.clean_name('a<b>:c?.jpg', ".jpg") == "abc" and e.clean_name("CON", ".jpg") == "_CON"
assert e.clean_name(" .. ", ".png") == ""
v = e.VideoInfo(60, 1920, 1080, 30, 1)
assert e.plan_video(v, 100 * e.MB, e.PRESETS["Media"])[2] is None
assert e.plan_video(v, 5 * e.MB, e.PRESETS["Media"])[2] < 1080
print("TUTTO OK")
