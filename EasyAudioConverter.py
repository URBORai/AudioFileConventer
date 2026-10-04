import os
import sys
import time
import shutil
import subprocess
import threading
import json
import ctypes
from ctypes import wintypes
from pathlib import Path
import tempfile
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, font
from concurrent.futures import ThreadPoolExecutor

# ========== 全域常數與系統設定 ==========
SETTINGS_FILE = "EasyConverterSettings.json"
MAX_PATH_LEN = 250
SUPPORTED_EXTS = {'.m4a', '.aac', '.caf', '.mp3', '.wav', '.flac', '.wma', '.ogg', '.mp4', '.mov', '.mkv', '.avi'}

# Windows API 常數
BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
CREATE_NO_WINDOW = 0x08000000
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ERROR_ALREADY_EXISTS = 183

# 執行緒控制標記
pause_event = threading.Event()
pause_event.set()  # Set = 正常執行, Clear = 暫停
stop_event = threading.Event()
global_error_msg = ""


# ========== 工具函式類 ==========
class WinAPIUtils:
    @staticmethod
    def setup_dpi_awareness():
        """設定高 DPI 感知，避免字體模糊"""
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass

    @staticmethod
    def prevent_sleep():
        """阻止系統休眠"""
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)

    @staticmethod
    def allow_sleep():
        """恢復系統自動休眠"""
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)

    @staticmethod
    def check_singleton():
        """使用 Mutex 確保只有單一實例執行"""
        mutex = ctypes.windll.kernel32.CreateMutexW(None, False, "Global\\EasyAudioConverter_Mutex")
        if ctypes.windll.kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
            messagebox.showerror("錯誤", "程式已經在執行中，請勿重複開啟！")
            sys.exit(0)
        return mutex

    @staticmethod
    def set_file_times(filepath, create_time, modify_time, access_time):
        """設定檔案建立/修改時間 (Windows API)"""
        def epoch_to_filetime(epoch):
            return int((epoch * 10000000) + 116444736000000000)

        class FILETIME(ctypes.Structure):
            _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]

        def to_ft(epoch):
            ft = FILETIME()
            val = epoch_to_filetime(epoch)
            ft.dwLowDateTime = val & 0xFFFFFFFF
            ft.dwHighDateTime = val >> 32
            return ft

        handle = ctypes.windll.kernel32.CreateFileW(filepath, 256, 0, None, 3, 128, None)
        if handle == -1:
            return
        ctime = to_ft(create_time)
        atime = to_ft(access_time)
        mtime = to_ft(modify_time)
        ctypes.windll.kernel32.SetFileTime(handle, ctypes.byref(ctime), ctypes.byref(atime), ctypes.byref(mtime))
        ctypes.windll.kernel32.CloseHandle(handle)

    @staticmethod
    def show_toast(title, message):
        """呼叫 PowerShell 顯示系統 Toast 通知"""
        ps_script = f"""
        [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
        $template = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
        $texts = $template.GetElementsByTagName("text")
        $texts[0].AppendChild($template.CreateTextNode("{title}")) > $null
        $texts[1].AppendChild($template.CreateTextNode("{message}")) > $null
        $toast = [Windows.UI.Notifications.ToastNotification]::new($template)
        $notifier = [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("影音轉檔工具")
        $notifier.Show($toast)
        """
        try:
            subprocess.Popen(["powershell", "-Command", ps_script], creationflags=CREATE_NO_WINDOW)
            import winsound
            winsound.MessageBeep()
        except Exception:
            pass


class FileUtils:
    @staticmethod
    def sanitize_filename(name):
        """將跨系統非法字元映射為全形符號"""
        mapping = {'?': '？', '*': '＊', ':': '：', '<': '＜', '>': '＞', '|': '｜', '"': '＂', '/': '／', '\\': '＼'}
        for k, v in mapping.items():
            name = name.replace(k, v)
        return name

    @staticmethod
    def check_disk_space(target_dir, required_bytes):
        """檢查磁碟剩餘空間"""
        try:
            free_bytes = ctypes.c_ulonglong(0)
            ctypes.windll.kernel32.GetDiskFreeSpaceExW(ctypes.c_wchar_p(target_dir), None, None, ctypes.byref(free_bytes))
            return free_bytes.value >= required_bytes
        except Exception:
            return True # 若無法檢查則放行

    @staticmethod
    def is_file_accessible(filepath):
        """嘗試開啟檔案1秒，檢查是否被雲端鎖定或未水合"""
        try:
            with open(filepath, 'rb') as f:
                f.read(1024)
            return True
        except Exception:
            return False

    @staticmethod
    def get_resource_path(filename):
        """取得資源路徑 (支援 PyInstaller 打包)"""
        if hasattr(sys, '_MEIPASS'):
            return os.path.join(sys._MEIPASS, filename)
        return filename

    @staticmethod
    def get_ffmpeg_path():
        """取得 FFmpeg 路徑 (支援 PyInstaller 打包)"""
        return FileUtils.get_resource_path('ffmpeg.exe'), FileUtils.get_resource_path('ffprobe.exe')


# ========== 轉檔引擎類 ==========
class ConverterEngine:
    def __init__(self, ui_callback, finish_callback):
        self.ui_callback = ui_callback
        self.finish_callback = finish_callback
        self.ffmpeg, self.ffprobe = FileUtils.get_ffmpeg_path()
        self.failed_list = []
        self.success_count = 0
        self.total_count = 0

    def check_ffmpeg_exists(self):
        if not os.path.exists(self.ffmpeg) or not os.path.exists(self.ffprobe):
            return False
        return True

    def get_media_duration(self, filepath):
        """呼叫 ffprobe 取得時長，可用於驗證 DRM 或損壞檔"""
        cmd = [self.ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", filepath]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, creationflags=CREATE_NO_WINDOW, timeout=5)
            return float(result.stdout.strip())
        except Exception:
            return -1.0

    def _trigger_io_pause(self, error_msg):
        """觸發 I/O 錯誤全域暫停"""
        global global_error_msg
        global_error_msg = error_msg
        pause_event.clear()
        self.ui_callback("PAUSE_REQUEST", error_msg)

    def convert_single_file(self, item, out_format, auto_vol, base_out_dir, base_src_dir=None):
        if stop_event.is_set():
            return False, "已取消"
        
        # 阻塞等待暫停恢復
        pause_event.wait()

        src_path = item['path']
        if not FileUtils.is_file_accessible(src_path):
            return False, "無法讀取 (可能被鎖定或雲端未下載)"
        
        duration = self.get_media_duration(src_path)
        if duration <= 0:
            return False, "檔案毀損或具備 DRM 版權保護"

        # 決定輸出路徑
        src_name = Path(src_path).stem
        safe_name = FileUtils.sanitize_filename(src_name) + out_format
        
        if base_src_dir and src_path.startswith(base_src_dir):
            # 樹狀結構重建
            rel_path = os.path.relpath(os.path.dirname(src_path), base_src_dir)
            if rel_path == '.':
                final_out_dir = base_out_dir
            else:
                final_out_dir = os.path.join(base_out_dir, rel_path)
        else:
            final_out_dir = base_out_dir

        try:
            os.makedirs(final_out_dir, exist_ok=True)
        except Exception as e:
            self._trigger_io_pause(f"無法建立資料夾 {final_out_dir}\n請檢查隨身碟是否斷線！\n{str(e)}")
            return False, "I/O 錯誤"

        final_out_path = os.path.join(final_out_dir, safe_name)
        
        # 防呆：MAX_PATH
        if len(final_out_path) > MAX_PATH_LEN:
            final_out_path = os.path.join(base_out_dir, safe_name) # 展平路徑
            if len(final_out_path) > MAX_PATH_LEN:
                safe_name = safe_name[:50] + out_format
                final_out_path = os.path.join(base_out_dir, safe_name)

        # 智慧續傳
        if os.path.exists(final_out_path) and os.path.getsize(final_out_path) > 0:
            return True, "已存在，跳過"

        # 本機快取再搬移
        temp_dir = tempfile.gettempdir()
        temp_out = os.path.join(temp_dir, f"temp_conv_{int(time.time())}_{threading.get_ident()}{out_format}")

        # 組裝 FFmpeg 參數
        cmd = [self.ffmpeg, "-y", "-i", src_path, "-vn", "-map_metadata", "0"]
        if auto_vol:
            cmd.extend(["-af", "loudnorm=I=-16:TP=-1.5:LRA=11"])
        
        if out_format == ".mp3":
            cmd.extend(["-c:a", "libmp3lame", "-q:a", "0"])
        elif out_format == ".wav":
            cmd.extend(["-c:a", "pcm_s16le"])
        elif out_format == ".flac":
            cmd.extend(["-c:a", "flac"])
        elif out_format in (".aac", ".m4a"):
            cmd.extend(["-c:a", "aac", "-b:a", "256k"])
        elif out_format == ".ogg":
            cmd.extend(["-c:a", "libvorbis", "-q:a", "5"])
        elif out_format == ".wma":
            cmd.extend(["-c:a", "wmav2", "-b:a", "192k"])
            
        cmd.append(temp_out)

        try:
            # 使用 BELOW_NORMAL_PRIORITY_CLASS 確保不卡死系統
            proc = subprocess.Popen(cmd, creationflags=BELOW_NORMAL_PRIORITY_CLASS | CREATE_NO_WINDOW, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            proc.communicate()
            
            if proc.returncode != 0:
                return False, "轉檔核心回報錯誤"

            # 雙重校驗
            if not os.path.exists(temp_out) or os.path.getsize(temp_out) < 10240: # < 10KB
                return False, "產出檔案過小或異常"
            
            out_duration = self.get_media_duration(temp_out)
            if out_duration < 0.5:
                return False, "產出音訊長度不足"

            # 繼承時間戳記
            stat = os.stat(src_path)
            WinAPIUtils.set_file_times(temp_out, stat.st_ctime, stat.st_mtime, stat.st_atime)

            # 搬移至最終目標
            pause_event.wait() # 搬移前再確認一次沒有被全域暫停
            shutil.move(temp_out, final_out_path)
            
            return True, "成功"

        except Exception as e:
            if "No such file or directory" in str(e) or "Permission denied" in str(e):
                self._trigger_io_pause(f"存取異常，請檢查隨身碟/硬碟是否鬆脫！\n錯誤: {e}")
                return False, "I/O 斷線"
            return False, str(e)
        finally:
            if os.path.exists(temp_out):
                try:
                    os.remove(temp_out)
                except:
                    pass

    def run_batch(self, file_list, out_format, auto_vol, out_dir):
        self.failed_list.clear()
        self.success_count = 0
        self.total_count = len(file_list)
        stop_event.clear()
        pause_event.set()

        WinAPIUtils.prevent_sleep()

        # 自動偵測核心數，保留1~2核
        max_workers = max(1, os.cpu_count() - 1)

        # 估算所需空間 (1.5倍)
        total_size = sum(os.path.getsize(f['path']) for f in file_list if os.path.exists(f['path']))
        req_space = total_size * 1.5
        if not FileUtils.check_disk_space(out_dir, req_space):
            self.ui_callback("ERROR", "目標硬碟剩餘空間不足！(需來源總大小 1.5 倍)")
            WinAPIUtils.allow_sleep()
            self.finish_callback()
            return

        # 建立時間戳記的根目錄
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        batch_out_dir = os.path.join(out_dir, f"轉檔完成_{timestamp}")
        
        processed = 0
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {}
            for item in file_list:
                futures[executor.submit(self.convert_single_file, item, out_format, auto_vol, batch_out_dir, getattr(item, 'base_dir', None))] = item

            for future in as_completed(futures):
                item = futures[future]
                try:
                    success, msg = future.result()
                    if success:
                        self.success_count += 1
                        self.ui_callback("SUCCESS", item['id'])
                    else:
                        self.failed_list.append((item['path'], msg))
                        self.ui_callback("FAIL", item['id'])
                except Exception as e:
                    self.failed_list.append((item['path'], str(e)))
                    self.ui_callback("FAIL", item['id'])
                
                processed += 1
                self.ui_callback("PROGRESS", f"{processed}/{self.total_count}")

        WinAPIUtils.allow_sleep()
        
        # 若資料夾為空，自動刪除
        try:
            if os.path.exists(batch_out_dir) and not os.listdir(batch_out_dir):
                os.rmdir(batch_out_dir)
        except:
            pass

        self.finish_callback()


# ========== 使用者介面 ==========
class AppUI(tk.Tk):
    def __init__(self):
        super().__init__()
        WinAPIUtils.setup_dpi_awareness()
        self.mutex = WinAPIUtils.check_singleton()

        self.title("長輩專用影音轉檔神器 (極度防呆版)")
        
        # 設定視窗圖示
        icon_path = FileUtils.get_resource_path("Gemini_Generated_Image_ja8ntsja8ntsja8n.ico")
        if os.path.exists(icon_path):
            try:
                self.iconbitmap(icon_path)
            except:
                pass

        self.geometry("1000x700")
        self.minsize(950, 600)
        self.configure(padx=20, pady=20)

        # 全域字型
        self.base_font_size = 14
        self.custom_font = font.Font(family="Microsoft JhengHei", size=self.base_font_size, weight="bold")
        self.small_font = font.Font(family="Microsoft JhengHei", size=max(9, int(self.base_font_size * 0.8)))
        self.option_add("*Font", self.custom_font)
        
        style = ttk.Style()
        style.configure("TButton", font=self.custom_font, padding=10)
        style.configure("TCheckbutton", font=self.custom_font)
        style.configure("TRadiobutton", font=self.custom_font)
        style.configure("TLabel", font=self.custom_font)
        style.configure("Treeview", font=self.custom_font, rowheight=int(self.base_font_size * 2.5))
        style.configure("Treeview.Heading", font=self.custom_font)

        self.file_list = []
        self.item_counter = 0
        self.settings = self.load_settings()

        self.setup_ui()
        self.engine = ConverterEngine(self.on_engine_update, self.on_engine_finish)

        if not self.engine.check_ffmpeg_exists():
            messagebox.showerror("嚴重錯誤", "找不到轉檔核心 (ffmpeg.exe)！\n請確認程式完整性。")
            self.destroy()

        self.protocol("WM_DELETE_WINDOW", self.on_closing)

    def load_settings(self):
        try:
            if os.path.exists(SETTINGS_FILE):
                with open(SETTINGS_FILE, 'r', encoding='utf-8') as f:
                    return json.load(f)
        except:
            pass
        return {"out_dir": "", "format": ".mp3", "adv_format": ".aac", "auto_vol": 0, "font_size": 14}

    def save_settings(self):
        self.settings["out_dir"] = self.out_dir_var.get()
        self.settings["format"] = self.format_var.get()
        self.settings["adv_format"] = getattr(self, 'adv_format_var', tk.StringVar(value=".aac")).get()
        self.settings["auto_vol"] = self.vol_var.get()
        self.settings["font_size"] = self.base_font_size
        try:
            with open(SETTINGS_FILE, 'w', encoding='utf-8') as f:
                json.dump(self.settings, f, ensure_ascii=False)
        except:
            pass

    def setup_ui(self):
        # 頂部工具列
        top_frame = ttk.Frame(self)
        top_frame.pack(fill=tk.X, pady=(0, 10))
        
        ttk.Label(top_frame, text="放大/縮小介面：").pack(side=tk.LEFT)
        ttk.Button(top_frame, text="➕ 放大", command=lambda: self.adjust_font(2)).pack(side=tk.LEFT, padx=5)
        ttk.Button(top_frame, text="➖ 縮小", command=lambda: self.adjust_font(-2)).pack(side=tk.LEFT, padx=5)

        # 主畫面分割
        main_paned = ttk.PanedWindow(self, orient=tk.HORIZONTAL)
        main_paned.pack(fill=tk.BOTH, expand=True)

        # 左側：日誌區
        left_frame = ttk.Frame(main_paned)
        main_paned.add(left_frame, weight=3)

        list_toolbar = ttk.Frame(left_frame)
        list_toolbar.pack(fill=tk.X, pady=5)
        
        btn_frame1 = ttk.Frame(list_toolbar)
        btn_frame1.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=2)
        ttk.Button(btn_frame1, text="📂 加入影音檔案", command=self.add_files).pack(fill=tk.X)
        ttk.Label(btn_frame1, text="*可一次選取多個檔案", foreground="gray", font=self.small_font, anchor="center").pack()

        btn_frame2 = ttk.Frame(list_toolbar)
        btn_frame2.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=2)
        ttk.Button(btn_frame2, text="📁 加入整個資料夾", command=self.add_folder).pack(fill=tk.X)
        ttk.Label(btn_frame2, text="*自動找出所有影音檔", foreground="gray", font=self.small_font, anchor="center").pack()

        self.log_text = tk.Text(left_frame, state='disabled', wrap='word', font=self.custom_font)
        scrollbar = ttk.Scrollbar(left_frame, orient=tk.VERTICAL, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scrollbar.set)
        
        self.log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        # 右側：設定與控制
        right_frame = ttk.Frame(main_paned)
        main_paned.add(right_frame, weight=2)

        # 輸出位置
        out_group = ttk.LabelFrame(right_frame, text="儲存位置", padding=10)
        out_group.pack(fill=tk.X, pady=10, padx=10)
        self.out_dir_var = tk.StringVar(value=self.settings.get("out_dir", ""))
        ttk.Entry(out_group, textvariable=self.out_dir_var, state="readonly").pack(fill=tk.X, pady=5)
        ttk.Button(out_group, text="更改儲存資料夾", command=self.select_out_dir).pack(fill=tk.X)
        ttk.Label(out_group, text="*轉檔完成後的音樂會存放在這裡", foreground="gray", font=self.small_font).pack(anchor=tk.W, pady=(5,0))

        # 格式與功能
        fmt_group = ttk.LabelFrame(right_frame, text="轉檔設定", padding=10)
        fmt_group.pack(fill=tk.X, pady=10, padx=10)
        
        self.format_var = tk.StringVar(value=self.settings.get("format", ".mp3"))
        
        def on_format_change():
            if self.format_var.get() == "advanced":
                self.adv_combo.config(state="readonly")
            else:
                self.adv_combo.config(state=tk.DISABLED)

        ttk.Radiobutton(fmt_group, text="轉成 MP3 (一般)", variable=self.format_var, value=".mp3", command=on_format_change).pack(anchor=tk.W, pady=(5,0))
        ttk.Label(fmt_group, text="*最通用的格式，所有手機/隨身碟都支援", foreground="gray", font=self.small_font).pack(anchor=tk.W, padx=25)

        ttk.Radiobutton(fmt_group, text="轉成 WAV (無損)", variable=self.format_var, value=".wav", command=on_format_change).pack(anchor=tk.W, pady=(5,0))
        ttk.Label(fmt_group, text="*音質最好，但檔案容量非常大", foreground="gray", font=self.small_font).pack(anchor=tk.W, padx=25)
        
        adv_frame = ttk.Frame(fmt_group)
        adv_frame.pack(anchor=tk.W, fill=tk.X, pady=(5,0))
        ttk.Radiobutton(adv_frame, text="其他格式：", variable=self.format_var, value="advanced", command=on_format_change).pack(side=tk.LEFT)
        
        self.adv_format_var = tk.StringVar(value=self.settings.get("adv_format", ".aac"))
        self.adv_combo = ttk.Combobox(adv_frame, textvariable=self.adv_format_var, values=[".aac", ".m4a", ".flac", ".ogg", ".wma"], width=6)
        self.adv_combo.pack(side=tk.LEFT, padx=2)
        on_format_change() # 初始化狀態

        self.vol_var = tk.IntVar(value=self.settings.get("auto_vol", 0))
        ttk.Checkbutton(fmt_group, text="自動放大音量", variable=self.vol_var).pack(anchor=tk.W, pady=(15,0))
        ttk.Label(fmt_group, text="*原本影片聲音太小的話，打勾會自動增強", foreground="gray", font=self.small_font).pack(anchor=tk.W, padx=25)

        # 狀態區
        self.status_var = tk.StringVar(value="準備就緒")
        ttk.Label(right_frame, textvariable=self.status_var, foreground="blue").pack(pady=20)

        self.btn_start = ttk.Button(right_frame, text="▶️ 開始轉檔", command=self.start_conversion)
        self.btn_start.pack(fill=tk.X, padx=10, pady=5, ipady=15)

        self.btn_stop = ttk.Button(right_frame, text="⏹️ 停止/取消", command=self.stop_conversion, state=tk.DISABLED)
        self.btn_stop.pack(fill=tk.X, padx=10, pady=5, ipady=15)
        
        self.btn_retry = ttk.Button(right_frame, text="🔄 重試失敗項目", command=self.retry_failed, state=tk.DISABLED)
        self.btn_retry.pack(fill=tk.X, padx=10, pady=5)

    def adjust_font(self, delta):
        new_size = self.base_font_size + delta
        if 10 <= new_size <= 30:
            self.base_font_size = new_size
            self.custom_font.configure(size=self.base_font_size)
            self.small_font.configure(size=max(9, int(self.base_font_size * 0.8)))
            style = ttk.Style()
            style.configure("Treeview", rowheight=int(self.base_font_size * 2.5))
            self.save_settings()

    def add_files(self):
        files = filedialog.askopenfilenames(title="選擇影音檔案")
        for f in files:
            if Path(f).suffix.lower() in SUPPORTED_EXTS:
                self.add_to_list(f)

    def add_folder(self):
        folder = filedialog.askdirectory(title="選擇包含影音的資料夾")
        if folder:
            for root, _, files in os.walk(folder):
                for file in files:
                    if Path(file).suffix.lower() in SUPPORTED_EXTS:
                        path = os.path.join(root, file)
                        self.add_to_list(path, base_dir=folder)

    def append_log(self, text):
        self.log_text.config(state='normal')
        self.log_text.insert(tk.END, text + "\n")
        self.log_text.see(tk.END)
        self.log_text.config(state='disabled')

    def add_to_list(self, path, base_dir=None):
        # 避免重複
        for existing in self.file_list:
            if existing['path'] == path:
                return
        
        item_id = f"item_{self.item_counter}"
        self.item_counter += 1
        
        self.file_list.append({'id': item_id, 'path': path, 'base_dir': base_dir, 'status': '等待中'})
        self.append_log(f"已加入檔案: {os.path.basename(path)}")

    def select_out_dir(self):
        d = filedialog.askdirectory(title="選擇儲存資料夾")
        if d:
            self.out_dir_var.set(d)
            self.save_settings()

    def start_conversion(self):
        if not self.file_list:
            messagebox.showwarning("提示", "請先加入檔案或資料夾！")
            return
        
        if not self.out_dir_var.get():
            messagebox.showwarning("提示", "請先選擇儲存位置！")
            return

        self.show_confirmation_dialog()

    def show_confirmation_dialog(self):
        top = tk.Toplevel(self)
        top.title("二次確認 - 待轉檔清單")
        top.geometry("800x500")
        top.transient(self)
        top.grab_set()

        # Toolbar in dialog
        toolbar = ttk.Frame(top)
        toolbar.pack(fill=tk.X, padx=10, pady=5)
        
        # Treeview in dialog
        columns = ("path", "status")
        tree = ttk.Treeview(top, columns=columns, show="headings", selectmode="extended")
        tree.heading("path", text="檔案路徑")
        tree.heading("status", text="狀態")
        tree.column("path", width=600)
        tree.column("status", width=100, anchor=tk.CENTER)
        
        scrollbar = ttk.Scrollbar(top, orient=tk.VERTICAL, command=tree.yview)
        tree.configure(yscroll=scrollbar.set)
        
        tree.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=10, pady=5)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        def remove_selected_from_dialog(event=None):
            selected = tree.selection()
            for iid in selected:
                tree.delete(iid)
                self.file_list = [item for item in self.file_list if item['id'] != iid]
                
        ttk.Button(toolbar, text="🗑️ 移除選取項目", command=remove_selected_from_dialog).pack(side=tk.LEFT)
        tree.bind("<Delete>", remove_selected_from_dialog)
        
        # Populate tree
        for item in self.file_list:
            tree.insert("", tk.END, iid=item['id'], values=(item['path'], item.get('status', '等待中')))

        def on_confirm():
            top.destroy()
            self._execute_conversion()

        # Bottom buttons
        btn_frame = ttk.Frame(top)
        btn_frame.pack(fill=tk.X, pady=10)
        ttk.Button(btn_frame, text="✅ 確認並開始轉檔", command=on_confirm).pack(side=tk.RIGHT, padx=10)
        ttk.Button(btn_frame, text="❌ 取消", command=top.destroy).pack(side=tk.RIGHT, padx=10)

    def _execute_conversion(self):
        if not self.file_list:
            self.append_log("轉檔清單為空，已取消轉檔。")
            return

        self.save_settings()
        self.btn_start.config(state=tk.DISABLED)
        self.btn_stop.config(state=tk.NORMAL)
        self.btn_retry.config(state=tk.DISABLED)
        
        for item in self.file_list:
            item['status'] = "處理中..."
        
        self.status_var.set("轉檔中，請稍候...")
        self.append_log("\n=== 開始轉檔 ===")
        
        actual_format = self.format_var.get()
        if actual_format == "advanced":
            actual_format = self.adv_format_var.get()
            
        threading.Thread(target=self.engine.run_batch, args=(
            self.file_list, actual_format, self.vol_var.get(), self.out_dir_var.get()
        ), daemon=True).start()

    def stop_conversion(self):
        if messagebox.askyesno("確認", "確定要取消目前的轉檔任務嗎？"):
            stop_event.set()
            self.status_var.set("正在取消中...")
            pause_event.set() # 若正在暫停，需喚醒讓其結束

    def on_engine_update(self, msg_type, content):
        self.after(0, self._process_update, msg_type, content)

    def _process_update(self, msg_type, content):
        if msg_type == "SUCCESS":
            path = next((item['path'] for item in self.file_list if item['id'] == content), content)
            self.append_log(f"✅ 成功: {os.path.basename(path)}")
            self.file_list = [item for item in self.file_list if item['id'] != content]
        elif msg_type == "FAIL":
            path = next((item['path'] for item in self.file_list if item['id'] == content), content)
            self.append_log(f"❌ 失敗: {os.path.basename(path)}")
            for item in self.file_list:
                if item['id'] == content:
                    item['status'] = '❌ 失敗'
        elif msg_type == "PROGRESS":
            self.status_var.set(f"轉檔進度: {content}")
        elif msg_type == "ERROR":
            self.append_log(f"⚠️ 錯誤: {content}")
            messagebox.showerror("錯誤", content)
        elif msg_type == "PAUSE_REQUEST":
            self.append_log(f"⏸️ 發生 I/O 錯誤，暫停中: {content}")
            self.show_io_error_dialog(content)

    def show_io_error_dialog(self, msg):
        top = tk.Toplevel(self)
        top.title("⚠️ 儲存裝置異常中斷 ⚠️")
        top.geometry("800x400")
        top.attributes("-topmost", True)
        top.grab_set()

        lbl = ttk.Label(top, text=msg + "\n\n請將隨身碟插回，或排除問題後點擊繼續。", foreground="red", justify=tk.CENTER)
        lbl.pack(expand=True, fill=tk.BOTH, padx=20, pady=20)

        def resume():
            pause_event.set()
            top.destroy()
        
        def abort():
            stop_event.set()
            pause_event.set()
            top.destroy()

        btn_frame = ttk.Frame(top)
        btn_frame.pack(pady=20)
        ttk.Button(btn_frame, text="✅ 我已插好，繼續執行", command=resume).pack(side=tk.LEFT, padx=10, ipady=10)
        ttk.Button(btn_frame, text="❌ 放棄本次任務", command=abort).pack(side=tk.LEFT, padx=10, ipady=10)

    def on_engine_finish(self):
        self.after(0, self._process_finish)

    def _process_finish(self):
        self.btn_start.config(state=tk.NORMAL)
        self.btn_stop.config(state=tk.DISABLED)
        
        if stop_event.is_set():
            self.status_var.set("已取消。")
            WinAPIUtils.show_toast("轉檔取消", "任務已手動終止。")
        else:
            if not self.engine.failed_list:
                self.status_var.set("✅ 所有檔案轉檔完成！")
                WinAPIUtils.show_toast("轉檔完成", "所有檔案已順利處理完畢！")
            else:
                self.status_var.set(f"⚠️ 完成，但有 {len(self.engine.failed_list)} 個失敗。")
                self.btn_retry.config(state=tk.NORMAL)
                self.export_failed_report()
                WinAPIUtils.show_toast("轉檔完成 (部分失敗)", "請查看桌面失敗清單報告。")

    def export_failed_report(self):
        desktop = os.path.join(os.path.join(os.environ['USERPROFILE']), 'Desktop')
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        report_path = os.path.join(desktop, f"轉檔失敗清單_{timestamp}.txt")
        try:
            with open(report_path, "w", encoding="utf-8") as f:
                f.write(f"--- 影音轉檔失敗報告 ({timestamp}) ---\n\n")
                for path, msg in self.engine.failed_list:
                    f.write(f"檔案: {path}\n原因: {msg}\n\n")
        except:
            pass

    def retry_failed(self):
        retry_list = [item for item in self.file_list if item.get('status') == '❌ 失敗']
        
        self.file_list = retry_list
        for item in self.file_list:
            item['status'] = '等待重試'
            
        self.show_confirmation_dialog()

    def on_closing(self):
        if not self.btn_start.instate(['!disabled']): # 正在轉檔
            if not messagebox.askyesno("警告", "轉檔正在進行中，確定要強制關閉程式嗎？\n這將會中斷所有進度！"):
                return
            stop_event.set()
            pause_event.set()
        
        self.save_settings()
        self.destroy()

if __name__ == "__main__":
    app = AppUI()
    app.mainloop()
