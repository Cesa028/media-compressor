# Media Compressor

Comprime **foto e video** scegliendo peso desiderato, qualità, formato e nome del risultato.
Programma per Windows, interfaccia chiara e scura. Tutto avviene sul computer, niente upload.

> **Accelerazione con scheda video: solo NVIDIA.** Con AMD, Intel o senza scheda dedicata il programma
> funziona lo stesso, ma i video vengono compressi dal processore e richiedono più tempo.

## Installazione
Scarica `MediaCompressor-Setup-<versione>.exe` dalle [Release](https://github.com/Cesa028/media-compressor/releases/latest) ed eseguilo (Windows 10/11, 64 bit).

## Come si usa
1. Trascina una o più foto o video nella finestra (o **Ctrl+O**). Con più file compare l'elenco, e il
   campo nome diventa l'aggiunta al nome di ciascuno.
2. Se vuoi, scegli la cartella finale (**Ctrl+D**). Vuota = stessa cartella dell'originale.
3. Imposta peso desiderato, qualità, formato e nome.
4. Premi **Comprimi** (**Ctrl+Invio**). **Esc** annulla.

L'originale non viene mai modificato; se il nome esiste già, il nuovo file diventa "nome (1)".

## Peso e qualità
- Peso **0** (nessun limite): decide solo la qualità.
- Peso impostato: è il massimo (nei video non supera mai il peso dell'originale). La qualità decide quanto sgranare prima di ridurre la risoluzione.
- PNG: Alta = senza perdita, Media = 256 colori, Bassa = 64 colori.

## Formati
- Foto: JPG, PNG, WEBP, HEIC/HEIF, BMP, TIFF → JPEG, WEBP, PNG.
- Animazioni: GIF, WEBP, PNG animati → WEBP, GIF, PNG (il PNG animato è senza perdita).
- Video: MP4, MOV, MKV, AVI, WEBM e altri → MP4 o MKV (H.264 / H.265), WEBM (VP9).

Nei video restano tutte le tracce audio. I sottotitoli di testo restano sempre; quelli a immagini
(Blu-ray, DVD) solo in MKV. I video HDR vengono convertiti in colori standard.

## Velocità
**Solo NVIDIA** (AMD e Intel non sono supportate). La casella "Usa la scheda video NVIDIA" rende la codifica MP4/MKV molte volte più
veloce, con una qualità appena inferiore a parità di peso. WEBM usa sempre il processore.

## Dal sorgente
```
pip install PySide6 Pillow pi-heif          # più ffmpeg in C:\ffmpeg o nel PATH
python media_compressor.py
python test_engine.py
python build.py                     # installer in dist\ (PyInstaller + Inno Setup 6)
```

## Licenza
MIT, © 2026 Cesa028. Vedi `licenses\THIRD-PARTY-NOTICES.txt`.
