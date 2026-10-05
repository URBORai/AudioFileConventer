# 影音轉檔神器 (EasyAudioConverter)

這是一個基於 Python 與 Tkinter 開發的圖形化影音轉檔工具，底層採用強大的 FFmpeg 作為轉檔核心。專為需要大量處理影音檔案的使用者設計，具備極度防呆的直覺操作介面。

## ✨ 主要功能特點

1. **拖曳加入 (Drag & Drop)**
   - 支援將檔案或整個資料夾直接拖曳進程式，會自動找出支援的影音檔案。
2. **多執行緒效能控制 (Thread Scaling)**
   - 內建 4 種運作模式設定（安靜、平衡、效能、滿速），可依需求調整佔用的 CPU 核心數。
3. **自動更名與防覆寫 (Auto Rename)**
   - 輸出若遇到同名檔案，會自動加上序號 (如 `video (1).mp3`)，絕不覆寫現有檔案。
4. **一鍵中斷與異常防護 (Instant Cancel & I/O Protection)**
   - 支援中途立即取消，並可安全結束所有 FFmpeg 程序。
   - 具備儲存裝置斷線防呆機制，隨身碟若不慎拔除會暫停並等待插回。
5. **多種轉檔格式支援**
   - 支援轉換為 MP3 (最通用)、WAV (無損)、AAC、M4A、FLAC、OGG、WMA 等常見音訊格式。
   - 具備「自動增強音量 (Volume Normalization)」選項，自動將太小聲的檔案調整至標準響度。
6. **友善介面設計**
   - 直覺的操作流程、檔案大小預估、詳細的執行紀錄 (Log) 顯示。
   - 支援字體大小縮放，照顧不同視力需求的使用者。
   - 轉檔失敗時，會自動在桌面產生詳細的錯誤報告清單。

## 🚀 如何使用

### 執行檔 (Windows 使用者)
進入 `dist` 資料夾，點擊 `影音轉檔神器.exe` 即可使用。
(程式已將 `ffmpeg.exe` 及 `ffprobe.exe` 內建打包於其中，不需額外安裝)

### 從原始碼執行
1. 請確認已安裝 Python 3.12 或以上版本。
2. 安裝必要套件 (包含支援拖曳功能的 TkinterDnD2)：
   ```bash
   pip install tkinterdnd2
   ```
3. 請確保 `ffmpeg.exe` 和 `ffprobe.exe` 放置於專案根目錄。
4. 執行程式：
   ```bash
   python EasyAudioConverter.py
   ```

## 🛠️ 自行打包 (PyInstaller)
若您修改了原始碼並想重新打包成 `.exe`，請在專案根目錄執行以下指令：

```bash
python build.py
```
這會使用預設設定自動呼叫 PyInstaller 進行打包，打包完成的執行檔將位於 `dist` 目錄中。

## 📁 專案架構
- `EasyAudioConverter.py`：主程式碼，包含 UI、FFmpeg 呼叫邏輯與多執行緒控制。
- `ffmpeg.exe` / `ffprobe.exe`：影音轉檔核心工具。
- `Gemini_Generated_Image_ja8ntsja8ntsja8n.ico`：應用程式圖示。
- `build.py`：自動化打包腳本。
- `dist/`：打包後的 `.exe` 發布資料夾。
