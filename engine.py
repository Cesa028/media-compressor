"""Media Compressor - logica di compressione: foto e animazioni (Pillow) e video (ffmpeg). Nessuna interfaccia."""
from __future__ import annotations

import io
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError
from pi_heif import register_heif_opener
from PySide6.QtCore import QThread, Signal

from common import IMAGE_EXT, NO_WINDOW, VIDEO_EXT, log, unique_path

MB = 1024 * 1024
register_heif_opener()                       # foto HEIC/HEIF dell'iPhone, lette come le altre

# formato -> estensione, per tipo di file
IMAGE_FORMATS = {"JPEG": ".jpg", "WEBP": ".webp", "PNG": ".png"}
ANIM_FORMATS = {"WEBP": ".webp", "GIF": ".gif", "PNG": ".png"}
VIDEO_FORMATS = {"MP4 (H.264)": ".mp4", "MP4 (H.265)": ".mp4", "MKV (H.264)": ".mkv", "MKV (H.265)": ".mkv",
                 "WEBM (VP9)": ".webm"}
FORMATS = {"photo": IMAGE_FORMATS, "anim": ANIM_FORMATS, "video": VIDEO_FORMATS}
TEXT_SUBS = {"subrip", "srt", "ass", "ssa", "mov_text", "webvtt", "text"}
MAX_ANIM_PIXELS = 400_000_000       # shortcut: i fotogrammi stanno tutti in RAM; oltre servirebbe farli passare da ffmpeg


@dataclass(frozen=True)
class Preset:
    q: int          # qualità foto senza peso desiderato
    min_q: int      # con peso desiderato: sotto questa qualità si riduce la risoluzione
    colors: int     # PNG: 0 = senza perdita
    anim_colors: int  # GIF
    crf: int        # video senza peso desiderato (riferimento H.264)
    bpp: float      # video con peso desiderato: bit per pixel minimi prima di ridurre la risoluzione


PRESETS = {"Alta": Preset(90, 80, 0, 256, 20, 0.08),
           "Media": Preset(75, 60, 256, 128, 26, 0.05),
           "Bassa": Preset(55, 30, 64, 64, 32, 0.025)}


class UserError(Exception):
    """Errore con un messaggio già comprensibile."""


class Cancelled(Exception):
    pass


def kind_of(path: Path) -> str | None:
    ext = path.suffix.lower()
    return "image" if ext in IMAGE_EXT else "video" if ext in VIDEO_EXT else None


def clean_name(name: str, ext: str) -> str:
    """Nome file valido su Windows, senza l'estensione (la aggiunge il programma)."""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name).strip().rstrip(". ")
    if name.lower().endswith(ext) or (ext == ".jpg" and name.lower().endswith(".jpeg")):
        name = name[:name.rfind(".")].rstrip(". ")
    if re.fullmatch(r"(?i)(con|prn|aux|nul|com\d|lpt\d)(\..*)?", name):
        name = f"_{name}"
    return name


# --------------------------------------------------------------------------- #
#  Foto e animazioni
# --------------------------------------------------------------------------- #
def load_image(path: Path, fmt: str) -> tuple[Image.Image, dict]:
    """Immagine già raddrizzata (EXIF) e in un modo adatto al formato, più i metadati da conservare."""
    with Image.open(path) as src:
        icc = src.info.get("icc_profile")
        im = ImageOps.exif_transpose(src)
        im.load()
    exif = im.getexif().tobytes()
    if im.mode == "CMYK":
        icc = None                                   # il profilo CMYK non vale per i pixel RGB
    if im.mode.startswith("I"):                      # 16 bit: senza scalare diventerebbe tutto bianco
        im = im.point(lambda v: v / 256).convert("L")
    alpha = "A" in im.mode or "transparency" in im.info
    if alpha:
        im = im.convert("RGBA")
        if fmt == "JPEG":                            # JPEG non ha trasparenza: sfondo bianco
            flat = Image.new("RGB", im.size, "white")
            flat.paste(im, mask=im.getchannel("A"))
            im = flat
    elif im.mode not in ("RGB", "L"):
        im = im.convert("RGB")
    meta = {}
    if icc:
        meta["icc_profile"] = icc
    if len(exif) > 8:
        meta["exif"] = exif
    return im, meta


def encode_image(im: Image.Image, fmt: str, quality: int, colors: int, meta: dict) -> bytes:
    buf = io.BytesIO()
    if fmt == "JPEG":
        im.save(buf, "JPEG", quality=quality, optimize=True, progressive=True, **meta)
    elif fmt == "WEBP":
        im.save(buf, "WEBP", quality=quality, method=4, **meta)
    else:
        if colors:
            im = im.quantize(colors, method=Image.Quantize.FASTOCTREE if im.mode == "RGBA" else None)
        im.save(buf, "PNG", optimize=True, **meta)
    return buf.getvalue()


def scaled(im: Image.Image, scale: float) -> Image.Image:
    if scale == 1.0:
        return im
    return im.resize((max(1, round(im.width * scale)), max(1, round(im.height * scale))), Image.Resampling.LANCZOS)


def fit(render, size: tuple[int, int], lossy: bool, preset: Preset, target: int, check, progress):
    """Cerca scala e qualità che stanno in `target` byte. render(scala, qualità) -> (dati, dimensioni)."""
    scale = 1.0
    for step in range(24):
        check()
        progress(min(0.9, step / 8))
        data, dims = render(scale, preset.min_q)
        if len(data) <= target:
            if lossy:                                # cerca la qualità più alta che sta nel peso
                lo, hi = preset.min_q + 1, 95
                while lo <= hi:
                    check()
                    mid = (lo + hi) // 2
                    trial = render(scale, mid)[0]
                    if len(trial) <= target:
                        data, lo = trial, mid + 1
                    else:
                        hi = mid - 1
            return data, dims
        if min(size) * scale <= 16:
            break
        scale *= min(0.9, max(0.5, math.sqrt(target / len(data)) * 0.95))
    raise UserError("Il peso desiderato è troppo piccolo per questa immagine. Aumentalo e riprova.")


def compress_image(src: Path, fmt: str, preset: Preset, target: int, check=lambda: None,
                   progress=lambda f: None) -> tuple[bytes, tuple[int, int]]:
    """Ritorna (dati, dimensioni). target in byte, 0 = nessun limite."""
    im, meta = load_image(src, fmt)
    if not target:
        return encode_image(im, fmt, preset.q, preset.colors, meta), im.size

    def render(scale, q):
        cur = scaled(im, scale)
        return encode_image(cur, fmt, q, preset.colors, meta), cur.size

    return fit(render, im.size, fmt != "PNG", preset, target, check, progress)


def compress_animation(src: Path, fmt: str, preset: Preset, target: int, check=lambda: None,
                       progress=lambda f: None) -> tuple[bytes, tuple[int, int]]:
    """Come compress_image, ma conserva tutti i fotogrammi, i tempi e la ripetizione."""
    frames, durations = [], []
    with Image.open(src) as im:
        if im.width * im.height * im.n_frames > MAX_ANIM_PIXELS:
            raise UserError("L'animazione è troppo grande (troppi fotogrammi o risoluzione troppo alta).")
        loop = im.info.get("loop")
        for i in range(im.n_frames):
            check()
            im.seek(i)
            frames.append(im.convert("RGBA"))
            durations.append(int(im.info.get("duration") or 100))
    alpha = any(f.getextrema()[3][0] < 255 for f in frames)
    if not alpha:
        frames = [f.convert("RGB") for f in frames]

    def render(scale, q):
        out = []
        for f in frames:
            check()
            f = scaled(f, scale)
            if fmt == "GIF" and not alpha and preset.anim_colors < 256:
                f = f.quantize(preset.anim_colors)
            out.append(f)
        opts = {"save_all": True, "append_images": out[1:], "duration": durations}
        if loop is not None or fmt != "GIF":         # GIF senza "loop" = si riproduce una volta sola
            opts["loop"] = loop if loop is not None else 1
        if fmt == "WEBP":
            opts.update(quality=q, method=4)
        elif fmt == "GIF":
            opts.update(optimize=True, disposal=2 if alpha else 1)
        buf = io.BytesIO()
        out[0].save(buf, fmt, **opts)
        return buf.getvalue(), out[0].size

    if not target:
        return render(1.0, preset.q)
    return fit(render, frames[0].size, fmt == "WEBP", preset, target, check, progress)


# --------------------------------------------------------------------------- #
#  Video
# --------------------------------------------------------------------------- #
def find_ffmpeg() -> str | None:
    here = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    for c in (here / "ffmpeg.exe", Path(r"C:\ffmpeg\ffmpeg.exe"), Path(r"C:\ffmpeg\bin\ffmpeg.exe")):
        if c.is_file():
            return str(c)
    return shutil.which("ffmpeg")


def run_ffmpeg_info(args: list[str]) -> subprocess.CompletedProcess:
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise UserError("ffmpeg non trovato: reinstalla il programma.")
    return subprocess.run([ffmpeg, "-hide_banner", *args], capture_output=True, text=True, encoding="utf-8",
                          errors="replace", creationflags=NO_WINDOW, timeout=60)


@lru_cache(maxsize=1)
def gpu_available() -> bool:
    """Vera prova di codifica con NVENC: l'encoder può essere elencato anche senza una scheda adatta."""
    # shortcut: solo NVIDIA; per AMD/Intel aggiungere qui le prove di *_amf / *_qsv
    try:
        return run_ffmpeg_info(["-loglevel", "error", "-f", "lavfi", "-i", "color=s=256x256:d=0.1",
                                "-c:v", "h264_nvenc", "-f", "null", "-"]).returncode == 0
    except (UserError, OSError, subprocess.TimeoutExpired):
        return False


@dataclass
class VideoInfo:
    duration: float
    width: int
    height: int
    fps: float
    audio: int = 0                  # numero di tracce audio
    subs: tuple = ()                # (indice dello stream, codec)
    hdr: str = ""                   # curva HDR ("smpte2084" = PQ, "arib-std-b67" = HLG)


def probe_video(path: Path) -> VideoInfo:
    out = run_ffmpeg_info(["-i", str(path)]).stderr
    dur = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", out)
    video = next((m.group(1) for m in re.finditer(r"Stream #0:\d+.*?: Video: ([^\n]+)", out)
                  if "attached pic" not in m.group(1)), None)
    size = video and re.search(r", (\d{2,5})x(\d{2,5})", video)
    if not (dur and size):
        raise UserError("Il file non sembra un video valido o è danneggiato.")
    h, m, s = dur.groups()
    fps = re.search(r"([\d.]+) fps", video)
    hdr = re.search(r"smpte2084|arib-std-b67", video)
    return VideoInfo(int(h) * 3600 + int(m) * 60 + float(s), int(size.group(1)), int(size.group(2)),
                     float(fps.group(1)) if fps else 30.0, len(re.findall(r"Stream #0:\d+.*?: Audio:", out)),
                     tuple((int(i), c) for i, c in re.findall(r"Stream #0:(\d+).*?: Subtitle: (\w+)", out)),
                     hdr.group(0) if hdr else "")


@dataclass
class Item:
    path: Path
    kind: str                       # "photo" | "anim" | "video"
    details: str
    size: int


def inspect(path: Path) -> Item:
    """Riconosce un file scelto dall'utente; solleva UserError se non è utilizzabile."""
    if not path.is_file():
        raise UserError("Il file non esiste: controlla il percorso.")
    kind = kind_of(path)
    if not kind:
        raise UserError("Formato non supportato. Scegli una foto (JPG, PNG, HEIC, WEBP, GIF, BMP, TIFF) "
                        "o un video (MP4, MOV, MKV, AVI, WEBM…).")
    try:
        if kind == "video":
            info = probe_video(path)
            sec = int(round(info.duration))
            hdr = "  ·  HDR" if info.hdr else ""
            return Item(path, "video", f"Video  ·  {info.width}×{info.height} px  ·  {sec // 60}:{sec % 60:02d}{hdr}",
                        path.stat().st_size)
        with Image.open(path) as im:
            frames = getattr(im, "n_frames", 1)
            if frames > 1 and im.format == "TIFF":
                raise UserError("I TIFF con più pagine non sono supportati: le altre pagine andrebbero perse.")
            if frames > 1 and im.format in ("GIF", "WEBP", "PNG"):
                return Item(path, "anim", f"Animazione  ·  {im.width}×{im.height} px  ·  {frames} fotogrammi",
                            path.stat().st_size)
            return Item(path, "photo", f"Foto  ·  {im.width}×{im.height} px", path.stat().st_size)
    except UserError:
        raise
    except Exception as e:  # noqa: BLE001
        log.warning("File non leggibile %s: %r", path, e)
        raise UserError("Il file non è leggibile o è danneggiato.") from e


def plan_video(info: VideoInfo, target: int, preset: Preset) -> tuple[int, int, int | None]:
    """Da peso desiderato a (kbps video, kbps per traccia audio, lato corto massimo o None)."""
    if info.duration <= 0:
        raise UserError("Non riesco a leggere la durata del video: imposta il peso a 0 e usa la qualità.")
    total = target * 8 * 0.97 / info.duration / 1000       # 3% di margine per il contenitore
    n = info.audio
    audio = 0 if not n else next((a for a in (128, 96, 64, 48) if a * n <= total * 0.15), 32)
    video = int(total - audio * n)
    if video < 50:
        minimum = (50 + audio * n) * 1000 * info.duration / 8 / 0.97 / MB
        raise UserError(f"Il peso desiderato è troppo piccolo per questo video: servono almeno "
                        f"{math.ceil(minimum * 10) / 10:g} MB.".replace(".", ",", 1))
    short, long = sorted((info.width, info.height))
    limit = None
    for side in (short, 1080, 720, 480, 360, 240):
        if side > short:
            continue
        limit = None if side == short else side
        if video * 1000 / (side * (side * long / short) * info.fps) >= preset.bpp:
            break
    return video, audio, limit


def video_filter(limit: int | None, hdr: str = "") -> str:
    tone = ""
    if hdr:                         # HDR -> SDR, altrimenti i colori escono slavati
        tone = (f"zscale=tin={hdr}:min=bt2020nc:pin=bt2020:t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,"
                "tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv,format=yuv420p,")
    if not limit:
        return tone + "scale=trunc(iw/2)*2:trunc(ih/2)*2"
    return tone + f"scale='if(gt(iw,ih),-2,{limit})':'if(gt(iw,ih),{limit},-2)'"


def stream_args(info: VideoInfo, fmt: str, audio_kbps: int) -> tuple[list[str], int]:
    """Tutte le tracce audio e i sottotitoli. Ritorna (argomenti, sottotitoli che il formato non può tenere)."""
    webm, mkv = fmt.startswith("WEBM"), fmt.startswith("MKV")
    args = ["-map", "0:a?", "-c:a", "libopus" if webm else "aac", "-b:a", f"{audio_kbps}k"]
    if webm:                        # libopus rifiuta disposizioni come 5.1(side)
        args += ["-af", "aformat=channel_layouts=7.1|5.1|stereo|mono"]
    kept = 0
    for index, codec in info.subs:
        if mkv:                     # MKV tiene anche i sottotitoli a immagini; mov_text va però convertito
            target = "srt" if codec == "mov_text" else "copy"
        elif codec in TEXT_SUBS:
            target = "webvtt" if webm else "mov_text"
        else:
            continue
        args += ["-map", f"0:{index}", f"-c:s:{kept}", target]
        kept += 1
    if mkv:
        args += ["-map", "0:t?", "-c:t", "copy"]        # font allegati dei sottotitoli
    return args, len(info.subs) - kept


def friendly_error(stderr: str) -> str:
    t = stderr.lower()
    for key, msg in (
            ("no space left", "Spazio su disco esaurito."),
            ("permission denied", "Non ho il permesso di leggere o scrivere il file."),
            ("invalid data found", "Il file è danneggiato o in un formato non riconosciuto."),
            ("moov atom not found", "Il file è incompleto o danneggiato."),
            ("no such file", "File non trovato: è stato spostato o eliminato?")):
        if key in t:
            return msg
    return "ffmpeg non è riuscito a comprimere questo video."


class VideoError(UserError):
    def __init__(self, stderr: str):
        super().__init__(friendly_error(stderr))
        self.detail = stderr[-4000:]


# --------------------------------------------------------------------------- #
#  Lavoro in thread
# --------------------------------------------------------------------------- #
@dataclass
class Job:
    src: Path
    out_dir: Path
    name: str
    fmt: str
    quality: str
    target_mb: float
    kind: str = "photo"
    gpu: bool = False


class ScanWorker(QThread):
    """Legge i file scelti senza bloccare la finestra."""
    scanned = Signal(list, list)             # [Item], [(percorso, messaggio)]

    def __init__(self, paths: list[Path]):
        super().__init__()
        self.paths = paths

    def run(self):
        items, errors = [], []
        for p in self.paths:
            if self.isInterruptionRequested():
                return
            try:
                items.append(inspect(p))
            except Exception as e:  # noqa: BLE001
                log.warning("File scartato %s: %r", p, e)
                errors.append((p, str(e) if isinstance(e, UserError) else "Il file non è leggibile."))
        if any(i.kind == "video" for i in items):
            gpu_available()                  # qui, così la finestra non si blocca: il risultato resta in cache
        self.scanned.emit(items, errors)


class CompressWorker(QThread):
    progress = Signal(float, str)            # frazione 0..1 di tutta la coda, testo di stato
    item_done = Signal(int, object, str)     # indice, percorso finale, nota
    item_failed = Signal(int, str, str)      # indice, messaggio, dettaglio
    cancelled = Signal()

    def __init__(self, jobs: list[Job]):
        super().__init__()
        self.jobs = jobs
        self.index = 0
        self.stop_requested = False
        self.proc: subprocess.Popen | None = None

    def stop(self):
        self.stop_requested = True
        if self.proc and self.proc.poll() is None:
            self.proc.kill()

    def _check(self):
        if self.stop_requested:
            raise Cancelled

    def _emit(self, part: float, text: str):
        n = len(self.jobs)
        if n > 1:
            text = f"File {self.index + 1} di {n}  ·  {text}"
        self.progress.emit((self.index + part) / n, text)

    def run(self):
        for self.index, job in enumerate(self.jobs):
            try:
                self._check()
                final, note = self._one(job)
                self.item_done.emit(self.index, final, note)
            except Cancelled:
                self.cancelled.emit()
                return
            except UserError as e:
                log.warning("%s: %s %s", job.src, e, getattr(e, "detail", ""))
                self.item_failed.emit(self.index, str(e), getattr(e, "detail", f"{job.src}"))
            except (UnidentifiedImageError, Image.DecompressionBombError) as e:
                log.warning("%s: %r", job.src, e)
                self.item_failed.emit(self.index, "Il file non è un'immagine valida, è danneggiato o è troppo grande.",
                                      f"{job.src}\n{e!r}")
            except Exception as e:  # noqa: BLE001
                log.exception("Compressione fallita: %s", job.src)
                msg = friendly_error(str(e)) if isinstance(e, OSError) else "Compressione non riuscita."
                if "ffmpeg" in msg:
                    msg = "Non è stato possibile leggere o salvare il file."
                self.item_failed.emit(self.index, msg, f"{job.src}\n{e!r}")

    def _one(self, job: Job) -> tuple[Path, str]:
        with tempfile.TemporaryDirectory(prefix="mc_", ignore_cleanup_errors=True) as tmp:
            ext = FORMATS[job.kind][job.fmt]
            work = Path(tmp) / f"out{ext}"
            target = int(job.target_mb * MB)
            note = (self._video(job, work, Path(tmp), target) if job.kind == "video"
                    else self._image(job, work, target))
            self._check()
            return self._publish(work, job.out_dir / f"{job.name}{ext}"), note

    @staticmethod
    def _publish(work: Path, wanted: Path) -> Path:
        """Sposta il risultato nella cartella finale senza mai sovrascrivere file esistenti."""
        final = unique_path(wanted)
        try:
            shutil.move(work, final)
        except OSError:
            if work.exists():                # copia a metà: non lasciare file rotti
                final.unlink(missing_ok=True)
            raise
        return final

    def _image(self, job: Job, work: Path, target: int) -> str:
        self._emit(0.05, "Comprimo l'immagine…")
        fn = compress_animation if job.kind == "anim" else compress_image
        data, size = fn(job.src, job.fmt, PRESETS[job.quality], target, self._check,
                        lambda f: self._emit(f, "Cerco la qualità migliore per il peso…"))
        work.write_bytes(data)
        return f"{size[0]}×{size[1]} px"

    def _video(self, job: Job, work: Path, tmp: Path, target: int) -> str:
        info = probe_video(job.src)
        if job.gpu and not job.fmt.startswith("WEBM") and gpu_available():   # NVENC non fa VP9
            try:
                return self._encode(job, info, work, tmp, target, True)
            except VideoError as e:
                log.warning("Scheda video non utilizzabile, ripiego sul processore: %s", e.detail)
        return self._encode(job, info, work, tmp, target, False)

    def _encode(self, job: Job, info: VideoInfo, work: Path, tmp: Path, target: int, gpu: bool) -> str:
        preset = PRESETS[job.quality]
        vp9, hevc, mp4 = job.fmt.startswith("WEBM"), "265" in job.fmt, job.fmt.startswith("MP4")
        if gpu:
            codec, speed = "hevc_nvenc" if hevc else "h264_nvenc", ["-preset", "p5"]
        elif vp9:
            codec, speed = "libvpx-vp9", ["-deadline", "good", "-cpu-used", "4", "-row-mt", "1"]
        else:
            codec, speed = "libx265" if hevc else "libx264", ["-preset", "medium"]
        base = [find_ffmpeg(), "-hide_banner", "-nostdin", "-y", "-loglevel", "error",
                *(["-hwaccel", "auto"] if gpu else []), "-i", str(job.src.resolve()), "-map", "0:V:0"]
        common = ["-c:v", codec, "-pix_fmt", "yuv420p", *speed]
        if info.hdr:
            common += ["-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709"]
        tail = ["-tag:v", "hvc1"] if hevc and mp4 else []
        if mp4:
            tail += ["-movflags", "+faststart"]
        note = "  ·  colori HDR convertiti in standard" if info.hdr else ""
        lost = stream_args(info, job.fmt, 128)[1]
        if lost:
            note += f"  ·  {lost} sottotitoli a immagini non copiati (scegli MKV per tenerli)"
        if gpu:
            note += "  ·  scheda video"
        where = " con la scheda video" if gpu else ""

        if not target:
            crf = preset.crf + (7 if vp9 else 4 if hevc else 0)
            rate = (["-rc", "vbr", "-cq", str(crf + 2), "-b:v", "0"] if gpu
                    else ["-crf", str(crf)] + (["-b:v", "0"] if vp9 else []))
            self._ffmpeg(base + ["-vf", video_filter(None, info.hdr)] + common + rate
                         + stream_args(info, job.fmt, 128)[0] + tail + [str(work)],
                         tmp, info.duration, 0.0, 1.0, f"Comprimo il video{where}…")
            return f"{info.width}×{info.height} px{note}"

        original = job.src.stat().st_size
        if target > original:            # mai gonfiare: il tetto è il peso dell'originale
            target = original
            note += "  ·  il peso richiesto superava l'originale: uso quello dell'originale"
        kbps, a_kbps, limit = plan_video(info, target, preset)
        for attempt in range(2):
            video = base + ["-vf", video_filter(limit, info.hdr)] + common + ["-b:v", f"{kbps}k"]
            output = stream_args(info, job.fmt, a_kbps or 32)[0] + tail + [str(work)]
            if gpu:                          # NVENC fa i due passaggi al suo interno
                self._ffmpeg(video + ["-rc", "vbr", "-multipass", "fullres", "-maxrate", f"{kbps * 3 // 2}k",
                                      "-bufsize", f"{kbps * 2}k"] + output,
                             tmp, info.duration, 0.0, 1.0, f"Comprimo il video{where}…")
            else:
                def passes(n):
                    if hevc:
                        return ["-x265-params", f"pass={n}:stats=x265.log:log-level=error"]
                    return ["-pass", str(n), "-passlogfile", "ffpass"]
                self._ffmpeg(video + passes(1) + ["-an", "-f", "null", os.devnull], tmp, info.duration,
                             0.0, 0.4, "Analizzo il video (passaggio 1 di 2)…")
                self._ffmpeg(video + passes(2) + output, tmp, info.duration,
                             0.4, 1.0, "Comprimo il video (passaggio 2 di 2)…")
            size = work.stat().st_size
            if size <= target or attempt:
                break
            kbps = max(30, int(kbps * target / size * 0.96))      # sforato: un solo nuovo tentativo
        short, long = sorted((info.width, info.height))
        dims = f"{info.width}×{info.height} px"
        if limit:
            other = round(limit * long / short / 2) * 2
            dims = f"ridotto a {other}×{limit} px" if info.width > info.height else f"ridotto a {limit}×{other} px"
        if size > target:
            note += "  ·  leggermente sopra il peso desiderato"
        return dims + note

    def _ffmpeg(self, args: list[str], tmp: Path, duration: float, start: float, end: float, text: str):
        self._check()
        errfile = tmp / "ffmpeg.err"
        began = time.monotonic()
        self._emit(start, text)
        with open(errfile, "wb") as err:
            self.proc = subprocess.Popen(args[:1] + ["-progress", "pipe:1", "-nostats"] + args[1:],
                                         cwd=tmp, stdout=subprocess.PIPE, stderr=err, creationflags=NO_WINDOW)
            for line in self.proc.stdout:
                if self.stop_requested:
                    self.proc.kill()
                    break
                if line.startswith(b"out_time_us=") and duration:
                    try:
                        part = min(1.0, max(0.0, int(line.split(b"=")[1]) / 1e6 / duration))
                    except ValueError:
                        continue
                    eta = ""
                    if part > 0.05:
                        left = (time.monotonic() - began) * (1 - part) / part
                        eta = f"  circa {int(left // 60)}:{int(left % 60):02d} rimanenti"
                    self._emit(start + (end - start) * part, text + eta)
            self.proc.wait()
        self._check()
        if self.proc.returncode:
            raise VideoError(errfile.read_text(encoding="utf-8", errors="replace"))
