# EasyAudioConverter (簡易影音轉檔器)

這是一個基於 Python 與 Tkinter 開發的圖形化影音轉檔工具，底層採用強大的 FFmpeg 作為轉檔核心。設計初衷是為了解決大量影音檔案轉檔時的使用者體驗問題，提供簡單、直覺且高效的批次處理能力。

## ✨ 主要功能特點

1. **直覺的拖曳操作 (Drag & Drop)**
   - 支援將單一檔案或整個資料夾直接從桌面拖曳進應用程式視窗，省去繁瑣的選取步驟。
2. **多執行緒效能控制 (Thread Scaling)**
   - 內建 4 種效能設定檔（安靜、平衡、效能、滿速），可自由調整同時進行轉檔的 CPU 核心數量。在高效轉檔的同時，不會讓電腦卡死。
3. **智慧檔案覆寫機制 (Auto Rename)**
   - 輸出的資料夾若已有同名檔案，程式會自動加上流水號（例如 `video (1).mp3`），不會覆寫原有檔案，方便進行重複測試。
4. **一秒終止 (Instant Cancel)**
   - 支援真正的立即停止轉檔，強制斬斷底層正在運作的 FFmpeg 程序，無需等待當前批次完成。
5. **豐富的格式支援**
   - 支援轉換為 MP3 (最通用)、WAV (無損)、AAC、M4A、FLAC、OGG、WMA 等多種常用音訊格式。
   - 內建「自動增強音量 (Volume Normalization)」選項，針對太小聲的錄音檔能自動放大至標準音量。
6. **完善的介面回饋**
   - 具備直覺的進度條、檔案大小預覽、詳細的執行紀錄 (Log)，以及支援字體大小縮放，照顧不同螢幕解析度的需求。

## 🚀 如何執行

### 直接執行 (Windows 使用者)
進入 `dist` 資料夾，直接雙擊執行 `簡易影音轉檔器.exe` 即可使用。
(程式已將 `ffmpeg.exe` 及 `ffprobe.exe` 封裝於單一執行檔中，無需額外安裝設定)

### 從原始碼執行
1. 請確認已安裝 Python 3.12 或以上版本。
2. 安裝必要的套件 (主要為支援拖曳功能的 TkinterDnD2)：
   ```bash
   pip install tkinterdnd2
   ```
3. 請確保 `ffmpeg.exe` 和 `ffprobe.exe` 放置於專案根目錄。
4. 執行主程式：
   ```bash
   python EasyAudioConverter.py
   ```

## 📦 如何自行打包 (PyInstaller)
若您修改了原始碼並想重新打包成單一 `.exe` 執行檔，請在專案根目錄下執行以下指令：

```bash
python -m PyInstaller EasyAudioConverter.py -n 簡易影音轉檔器 --windowed --onefile --icon=Gemini_Generated_Image_ja8ntsja8ntsja8n.ico --collect-data tkinterdnd2 --hidden-import tkinterdnd2 --add-data "ffmpeg.exe;." --add-data "ffprobe.exe;." --noconfirm --clean
```

## 📂 專案架構
- `EasyAudioConverter.py`：主程式碼，包含 UI 介面、FFmpeg 調用邏輯與多執行緒排程。
- `ffmpeg.exe` / `ffprobe.exe`：影音轉檔核心工具 (必要)。
- `Gemini_Generated_Image_ja8ntsja8ntsja8n.ico`：應用程式圖示。
- `dist/`：打包後的 `.exe` 發布資料夾。
