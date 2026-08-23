<div align="center">

# 🎮 Bman Downloader

**A one-stop desktop app for finding, converting, and installing Xbox 360 & OG Xbox games straight to your modded console.**

Search a massive library, download at full speed, auto-convert to the format your console wants, and send it over FTP — all without leaving the app.

![Platform](https://img.shields.io/badge/platform-Windows-0078D6?logo=windows&logoColor=white)
![Python](https://img.shields.io/badge/built%20with-Python-3776AB?logo=python&logoColor=white)
![Version](https://img.shields.io/github/v/release/BmanGames024/bman-downloader?label=latest)
![Downloads](https://img.shields.io/github/downloads/BmanGames024/bman-downloader/total)
![Stars](https://img.shields.io/github/stars/BmanGames024/bman-downloader?style=flat)

</div>

---

## ✨ What it does

Bman Downloader takes you from *"I want this game"* to *"it's on my console"* in a few clicks. No juggling separate download tools, ISO converters, and FTP clients — it's all built in.

<!-- Tip: drop a screenshot or GIF of the app here. -->
<!-- ![App screenshot](docs/screenshot.png) -->

---

## 📥 Installation

### Easiest — grab the release
1. Head to the [**Releases**](https://github.com/BmanGames024/bman-downloader/releases/latest) page.
2. Download the latest `BmanDownloader.exe`.
3. Run it. — no install needed.
4. App may take a minute on first start to cache Archive items
5. Head to [**Archive.org**](https://archive.org/)
6. Open inspect element
7. If on Firefox, go to the Storage tab, cookies, and put the login user and sig, in the settings of the app (this is required to download)
8. If on Chrome, go to the Application tab, cookies, and put the login user and sig, in the settings of the app (this is required to download)
> **Heads up on antivirus:** Windows Defender may show a false positive. If it does, allow the file or grab it fresh from the official Releases link above.

### Run from source
```bash
git clone https://github.com/BmanGames024/bman-downloader.git
cd bman-downloader
python temu.py
```
---

## 🎯 Quick start

1. **Search** for a game in the Library tab.
2. Pick your **conversion format** (GOD, XeX, or extract-only).
3. *(Optional)* Enter your console's FTP details in **Settings** and tick **Upload via FTP**.
4. Hit **Download** — the app fetches, converts, and (if enabled) sends it to your console automatically.
5. Watch progress in the **Downloads** tab and the full log in **Logs**.

### Setting up FTP
Open **Settings → FTP** and enter your console's IP address (default user/pass is usually `xbox` / `xbox`). Use the built-in **FTP browser** to test the connection and pick your install folder.

---

## 🚀 Features

### 🔍 Massive built-in library & search
- Search **thousands of Xbox 360 and Original Xbox** titles pulled from multiple archive.org collections — full games, **XBLA**, **XBLIG**, and **DLC**.
- Instant, smart search with live results and game details.
- **Cover art** fetched automatically from SteamGridDB so you can see what you're grabbing.
- The whole library index is **cached locally**, so every launch after the first is effectively instant.

### 🌍 Powerful filtering
- **Region filter** — All / Region-Free / USA (NTSC-U) / Europe (PAL) / Japan (NTSC-J). Reads No-Intro/Redump-style tags *and* scene release names.
- **Show / hide DLC** with a single toggle so full games and add-ons never clutter each other.

### ⬇️ Fast, resilient downloads
- **Segmented multi-connection downloading** — up to **16 parallel connections per file** for maximum speed.
- **Automatic retries with exponential backoff** — flaky connections and rate limits recover on their own instead of failing the whole download.
- Fully **tunable** in Settings: connection count, segment sizes, retry limits, and backoff — with sensible defaults if you'd rather not touch a thing.

### 🔄 Built-in format conversion
No external tools to install — conversion is baked right in:
- **ISO → GOD** (Games on Demand) via ISO2GOD.
- **ISO → XeX** (extracted game folder) via extract-xiso.
- **Extract-only** mode when you just want the raw files.
- **Bulk convert everything** with one click — **GOD All** or **XEX All** rips through your whole download folder automatically.

### 📡 Direct FTP install to your console
- Send games **straight to your modded Xbox 360 or OG Xbox over FTP** — DashLaunch, Aurora, FSD, and the OG dashboards are all supported.
- Uses **active-mode FTP** (what console servers actually expect) with **large block transfers** for speed.
- **Parallel uploads** — up to 4 simultaneous connections.
- **Built-in FTP file browser** — navigate your console's drive, create folders, and pick exactly where a game lands.
- **Smart content detection** — automatically finds the console-native Title ID folder (e.g. `584E07D2`) inside an archive and uploads *only* what the console needs, dropping readmes, cover art, and wrapper folders.
- **Download → convert → upload in one shot** — tick "Upload via FTP" and the app handles the whole chain, or send an entire library with **Upload All via FTP**.

### 🧩 One-click Xefu / HDD compatibility install
- The **Install Xefu** button sets up HDD compatibility on your console automatically — it reads your drive layout and installs the right compatibility pack for your setup.

### 🔔 Automatic updates
- Checks for new releases on launch and offers a **one-click update** — downloads the new build, swaps it in, and restarts itself. No manual reinstalling.

### 🖥️ Clean, modern interface
- Dark-mode UI built with CustomTkinter.
- Simple sidebar navigation: **Library / Search**, **Downloads**, **Logs**, and **Settings**.
- Live progress, speeds, and a full activity log for every download and upload.
- Your settings and FTP config persist between launches.

## ⚙️ Tunable settings

| Setting | What it does |
|---|---|
| **Parallel connections** | Connections opened per file (1–16). More = faster, to a point. |
| **Min size per segment** | How much file each extra connection earns. |
| **Min file size to split** | Files below this download as a single stream. |
| **Retries per segment** | How many times a stalled segment retries before failing. |
| **Retry backoff** | How long to wait between retries — raise it for flaky connections. |
| **FTP parallel uploads** | Simultaneous FTP connections (1–4). |
| **Region filter** | Which regions show in search results. |

---

## ⚠️ Disclaimer

This tool is intended for creating and installing backups of games you legally own, and for preserving software on hardware you own. You are responsible for complying with the laws in your country regarding game backups and copyrighted material. The author does not host any game files — all content is retrieved from third-party archives.

---

<div align="center">

Made with ❤️ for the Xbox 360 modding community by **[BmanGames024](https://github.com/BmanGames024)**

⭐ Star the repo if it saved you some time!

</div>
