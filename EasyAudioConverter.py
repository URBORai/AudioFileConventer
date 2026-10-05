import os
import sys
import time
import datetime
import shutil
import subprocess
import threading
import json
import ctypes
from ctypes import wintypes
from pathlib import Path
import tempfile
import uuid
import tkinter as tk
try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    has_dnd = True
except ImportError:
    has_dnd = False
from tkinter import ttk, filedialog, messagebox, font
from concurrent.futures import ThreadPoolExecutor, as_completed

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
    def format_size(size_bytes):
        if size_bytes == 0:
            return "0 B"
        size_name = ("B", "KB", "MB", "GB", "TB")
        import math
        i = int(math.floor(math.log(size_bytes, 1024)))
        p = math.pow(1024, i)
        s = round(size_bytes / p, 2)
        return f"{s} {size_name[i]}"

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
        
        self.ui_callback("START", item['id'])
        pause_event.wait()

        src_path = item['path']
        if not FileUtils.is_file_accessible(src_path):
            return False, "無法讀取 (可能被佔用或權限不足)"
        
        if Path(src_path).suffix.lower() == out_format.lower() and not auto_vol:
            return True, "格式相同已略過"
        
        src_name = Path(src_path).stem
        original_safe_name = FileUtils.sanitize_filename(src_name)
        safe_name = original_safe_name + out_format
        
        if base_src_dir and src_path.startswith(base_src_dir):
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
            self._trigger_io_pause(f"無法建立資料夾 {final_out_dir}\n檢查是否為磁碟機斷線！\n{str(e)}")
            return False, "I/O 錯誤"

        final_out_path = os.path.join(final_out_dir, safe_name)
        if len(final_out_path) > MAX_PATH_LEN:
            final_out_path = os.path.join(base_out_dir, safe_name)
            if len(final_out_path) > MAX_PATH_LEN:
                original_safe_name = original_safe_name[:50]
                safe_name = original_safe_name + out_format
                final_out_path = os.path.join(base_out_dir, safe_name)

        counter = 1
        while os.path.exists(final_out_path):
            safe_name = f"{original_safe_name} ({counter}){out_format}"
            final_out_path = os.path.join(final_out_dir, safe_name)
            counter += 1

        temp_dir = tempfile.gettempdir()
        temp_out = os.path.join(temp_dir, f"temp_conv_{uuid.uuid4().hex}{out_format}")

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
        move_success = False
        try:
            proc = subprocess.Popen(cmd, creationflags=BELOW_NORMAL_PRIORITY_CLASS | CREATE_NO_WINDOW, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='ignore')
            while True:
                if stop_event.is_set():
                    proc.terminate()
                    proc.wait()
                    return False, "已取消"
                try:
                    out, err = proc.communicate(timeout=0.5)
                    break
                except subprocess.TimeoutExpired:
                    continue
            
            if proc.returncode != 0:
                return False, "轉檔引擎回報錯誤"

            if not os.path.exists(temp_out):
                return False, "無法產生檔案 (來源檔損毀或不含音軌)"
            
            stat = os.stat(src_path)
            WinAPIUtils.set_file_times(temp_out, stat.st_ctime, stat.st_mtime, stat.st_atime)

            pause_event.wait()
            shutil.move(temp_out, final_out_path)
            move_success = True
            return True, "成功"
        except Exception as e:
            if "No such file or directory" in str(e) or "Permission denied" in str(e):
                self._trigger_io_pause(f"來源異常，檢查是否已斷線\n錯誤: {e}")
                return False, "I/O 斷線"
            return False, str(e)
        finally:
            if not move_success and os.path.exists(temp_out):
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
        max_workers = getattr(self, "max_workers", max(1, int((os.cpu_count() or 1) * 0.75)))

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
                futures[executor.submit(self.convert_single_file, item, out_format, auto_vol, batch_out_dir, item.get('base_dir'))] = item

            for future in as_completed(futures):
                item = futures[future]
                try:
                    success, msg = future.result()
                    if success:
                        self.success_count += 1
                        self.ui_callback("SUCCESS", (item['id'], msg))
                    else:
                        self.failed_list.append((item['path'], msg))
                        self.ui_callback("FAIL", (item['id'], msg))
                except Exception as e:
                    self.failed_list.append((item['path'], str(e)))
                    self.ui_callback("FAIL", (item['id'], str(e)))
                
                processed += 1
                self.ui_callback("PROGRESS", (processed, self.total_count))

        WinAPIUtils.allow_sleep()
        
        # 若資料夾為空，自動刪除
        try:
            if os.path.exists(batch_out_dir) and not os.listdir(batch_out_dir):
                os.rmdir(batch_out_dir)
        except:
            pass

        self.finish_callback()


# ========== 使用者介面 ==========
class AppUI(TkinterDnD.Tk if has_dnd else tk.Tk):
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
        self.max_workers = self.settings.get('max_workers', max(1, int((os.cpu_count() or 1) * 0.75)))

        self.setup_ui()
        if has_dnd:
            self.drop_target_register(DND_FILES)
            self.dnd_bind("<<Drop>>", self.on_drop)
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
        top_frame = ttk.Frame(self)
        top_frame.pack(fill=tk.X, pady=(0, 5))
        ttk.Label(top_frame, text="字體大小:").pack(side=tk.LEFT, padx=(10, 0))
        ttk.Button(top_frame, text="放大", command=lambda: self.adjust_font(2)).pack(side=tk.LEFT, padx=5)
        ttk.Button(top_frame, text="縮小", command=lambda: self.adjust_font(-2)).pack(side=tk.LEFT, padx=5)
        ttk.Button(top_frame, text="⚙️ 設定", command=self.open_settings).pack(side=tk.RIGHT, padx=10)

        main_paned = ttk.PanedWindow(self, orient=tk.VERTICAL)
        main_paned.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)

        controls_frame = ttk.Frame(main_paned)
        main_paned.add(controls_frame, weight=1)

        left_col = ttk.Frame(controls_frame)
        left_col.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 5))
        right_col = ttk.Frame(controls_frame)
        right_col.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True, padx=(5, 0))

        add_group = ttk.LabelFrame(left_col, text="第一步：選擇檔案", padding=10)
        add_group.pack(fill=tk.X, pady=(0, 10))
        btn_frame1 = ttk.Frame(add_group)
        btn_frame1.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=2)
        ttk.Button(btn_frame1, text="加入影音檔案", command=self.add_files).pack(fill=tk.X, ipady=5)
        ttk.Label(btn_frame1, text="*可拖曳或多選", foreground="gray", font=self.small_font, anchor="center").pack()

        btn_frame2 = ttk.Frame(add_group)
        btn_frame2.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=2)
        ttk.Button(btn_frame2, text="加入整個資料夾", command=self.add_folder).pack(fill=tk.X, ipady=5)
        ttk.Label(btn_frame2, text="*自動找出所有影音", foreground="gray", font=self.small_font, anchor="center").pack()

        out_group = ttk.LabelFrame(left_col, text="第二步：儲存位置", padding=10)
        out_group.pack(fill=tk.X, pady=(0, 10))
        self.out_dir_var = tk.StringVar(value=self.settings.get("out_dir", ""))
        out_entry_frame = ttk.Frame(out_group)
        out_entry_frame.pack(fill=tk.X)
        ttk.Entry(out_entry_frame, textvariable=self.out_dir_var, state="readonly").pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 5))
        ttk.Button(out_entry_frame, text="瀏覽...", command=self.select_out_dir, width=8).pack(side=tk.RIGHT)

        fmt_group = ttk.LabelFrame(left_col, text="第三步：格式設定", padding=10)
        fmt_group.pack(fill=tk.X)
        self.format_var = tk.StringVar(value=self.settings.get("format", ".mp3"))
        
        def on_format_change():
            if self.format_var.get() == "advanced":
                self.adv_combo.config(state="readonly")
            else:
                self.adv_combo.config(state=tk.DISABLED)

        ttk.Radiobutton(fmt_group, text="轉成 MP3 (最通用)", variable=self.format_var, value=".mp3", command=on_format_change).pack(anchor=tk.W)
        ttk.Radiobutton(fmt_group, text="轉成 WAV (無損音質)", variable=self.format_var, value=".wav", command=on_format_change).pack(anchor=tk.W, pady=5)
        
        adv_frame = ttk.Frame(fmt_group)
        adv_frame.pack(anchor=tk.W, fill=tk.X)
        ttk.Radiobutton(adv_frame, text="其他格式:", variable=self.format_var, value="advanced", command=on_format_change).pack(side=tk.LEFT)
        self.adv_format_var = tk.StringVar(value=self.settings.get("adv_format", ".aac"))
        self.adv_combo = ttk.Combobox(adv_frame, textvariable=self.adv_format_var, values=[".aac", ".m4a", ".flac", ".ogg", ".wma"], width=6)
        self.adv_combo.pack(side=tk.LEFT, padx=5)
        on_format_change()
        self.vol_var = tk.IntVar(value=self.settings.get("auto_vol", 0))
        ttk.Checkbutton(fmt_group, text="自動增強音量", variable=self.vol_var).pack(anchor=tk.W, pady=(10,0))

        action_group = ttk.LabelFrame(right_col, text="執行區", padding=20)
        action_group.pack(fill=tk.BOTH, expand=True)
        self.status_var = tk.StringVar(value="準備就緒，請先加入檔案")
        status_label = ttk.Label(action_group, textvariable=self.status_var, foreground="blue", font=self.custom_font, anchor="center")
        status_label.pack(fill=tk.X, pady=(10, 10))
        self.progress_var = tk.DoubleVar(value=0)
        self.progress_bar = ttk.Progressbar(action_group, variable=self.progress_var, maximum=100)
        self.progress_bar.pack(fill=tk.X, pady=(0, 30))
        self.btn_start = ttk.Button(action_group, text="開始轉檔", command=self.start_conversion)
        self.btn_start.pack(fill=tk.X, pady=5, ipady=15)
        self.btn_stop = ttk.Button(action_group, text="停止轉檔", command=self.stop_conversion, state=tk.DISABLED)
        self.btn_stop.pack(fill=tk.X, pady=5, ipady=10)
        self.btn_retry = ttk.Button(action_group, text="重試失敗項目", command=self.retry_failed, state=tk.DISABLED)
        self.btn_retry.pack(fill=tk.X, pady=5)

        log_frame = ttk.LabelFrame(main_paned, text="詳細執行紀錄 (Log)")
        main_paned.add(log_frame, weight=1)
        self.log_text = tk.Text(log_frame, state='disabled', wrap='word', font=self.custom_font, height=8)
        scrollbar = ttk.Scrollbar(log_frame, orient=tk.VERTICAL, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scrollbar.set)
        self.log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=5, pady=5)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y, pady=5)

    
    def open_settings(self):
        try:
            top = tk.Toplevel(self)
            top.title("效能設定")
            top.geometry("450x300")
            top.transient(self)
            top.grab_set()
            
            ttk.Label(top, text="選擇運作模式或手動調整核心數：", font=self.custom_font).pack(pady=(15,5))
            
            max_c = os.cpu_count() or 4
            var = tk.IntVar(value=int(self.max_workers))
            
            mode_frame = ttk.Frame(top)
            mode_frame.pack(fill=tk.X, padx=20, pady=5)
            
            def set_preset(val):
                var.set(val)
                lbl.config(text=f"{val} 核心")
                
            ttk.Button(mode_frame, text="安靜 (25%)", command=lambda: set_preset(max(1, int(max_c * 0.25)))).pack(side=tk.LEFT, expand=True, padx=2)
            ttk.Button(mode_frame, text="平衡 (50%)", command=lambda: set_preset(max(1, int(max_c * 0.5)))).pack(side=tk.LEFT, expand=True, padx=2)
            ttk.Button(mode_frame, text="效能 (75%)", command=lambda: set_preset(max(1, int(max_c * 0.75)))).pack(side=tk.LEFT, expand=True, padx=2)
            ttk.Button(mode_frame, text="滿速 (100%)", command=lambda: set_preset(max_c)).pack(side=tk.LEFT, expand=True, padx=2)
            
            scale = tk.Scale(top, from_=1, to=max_c, orient=tk.HORIZONTAL, variable=var, length=300)
            scale.pack(fill=tk.X, padx=20, pady=(15, 0))
            
            lbl = ttk.Label(top, text=f"{var.get()} 核心", font=self.custom_font)
            lbl.pack(pady=5)
            
            def on_change(*args):
                lbl.config(text=f"{var.get()} 核心")
            var.trace_add("write", on_change)
            
            def save():
                self.max_workers = var.get()
                self.settings["max_workers"] = self.max_workers
                self.save_settings()
                top.destroy()
                
            ttk.Button(top, text="儲存設定", command=save, padding=5).pack(pady=10)
        except Exception as e:
            import traceback
            with open('debug_settings.txt', 'w') as df:
                traceback.print_exc(file=df)

    def on_drop(self, event):
        files = self.tk.splitlist(event.data)
        for f in files:
            if os.path.isdir(f):
                for root, _, _files in os.walk(f):
                    for _file in _files:
                        if Path(_file).suffix.lower() in SUPPORTED_EXTS:
                            self.add_to_list(os.path.join(root, _file), base_dir=f)
            else:
                if Path(f).suffix.lower() in SUPPORTED_EXTS:
                    self.add_to_list(f)

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
        
        try:
            size_mb = os.path.getsize(path) / (1024 * 1024)
            size_str = f"{size_mb:.2f} MB"
        except Exception:
            size_str = "未知"
            
        self.file_list.append({'id': item_id, 'path': path, 'base_dir': base_dir, 'status': '等待中', 'size': size_str})
        self.append_log(f"已加入檔案: {os.path.basename(path)} ({size_str})")

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

        btn_frame = ttk.Frame(top)
        btn_frame.pack(side=tk.BOTTOM, fill=tk.X, pady=10)

        toolbar = ttk.Frame(top)
        toolbar.pack(side=tk.TOP, fill=tk.X, padx=10, pady=5)
        
        tree_frame = ttk.Frame(top)
        tree_frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=10, pady=5)
        
        columns = ("path", "size", "status")
        tree = ttk.Treeview(tree_frame, columns=columns, show="headings", selectmode="extended")
        tree.heading("path", text="檔案路徑")
        tree.heading("size", text="大小")
        tree.heading("status", text="狀態")
        
        tree.column("path", width=450)
        tree.column("size", width=100, anchor=tk.CENTER)
        tree.column("status", width=120, anchor=tk.CENTER)
        
        scrollbar = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL, command=tree.yview)
        tree.configure(yscroll=scrollbar.set)
        
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        def remove_selected_from_dialog(event=None):
            selected = tree.selection()
            for iid in selected:
                tree.delete(iid)
                self.file_list = [item for item in self.file_list if item['id'] != iid]
                
        ttk.Button(toolbar, text="移除選取項目", command=remove_selected_from_dialog).pack(side=tk.LEFT)
        tree.bind("<Delete>", remove_selected_from_dialog)
        
        for item in self.file_list:
            tree.insert("", tk.END, iid=item['id'], values=(item['path'], item.get('size', '未知'), item.get('status', '等待中')))
        
        def on_confirm():
            top.destroy()
            if self.file_list:
                self.log_text.config(state='normal')
                self.log_text.delete('1.0', tk.END)
                self.log_text.config(state='disabled')
                self._execute_conversion()

        tk.Button(btn_frame, text="確認轉檔", command=on_confirm, font=self.custom_font, bg="#d4edda", padx=10, pady=5).pack(side=tk.RIGHT, padx=10)
        tk.Button(btn_frame, text="取消", command=top.destroy, font=self.custom_font, bg="#f8d7da", padx=10, pady=5).pack(side=tk.RIGHT, padx=5)

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
        self.progress_var.set(0)
        self.append_log("\n=== 開始轉檔 ===")
        
        actual_format = self.format_var.get()
        if actual_format == "advanced":
            actual_format = self.adv_format_var.get()
            
        self.engine.max_workers = getattr(self, "max_workers", max(1, int((os.cpu_count() or 1) * 0.75)))
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
        if msg_type == "START":
            path = next((item['path'] for item in self.file_list if item['id'] == content), content)
            self.append_log(f"[開始] 正在處理: {os.path.basename(path)}")
        elif msg_type == "SUCCESS":
            item_id, msg = content
            path = next((item['path'] for item in self.file_list if item['id'] == item_id), item_id)
            if msg == "格式相同已略過":
                self.append_log(f"[略過] {os.path.basename(path)} - 格式相同")
            else:
                self.append_log(f"[成功] {os.path.basename(path)}")
            self.file_list = [item for item in self.file_list if item['id'] != item_id]
        elif msg_type == "FAIL":
            item_id, msg = content
            path = next((item['path'] for item in self.file_list if item['id'] == item_id), item_id)
            self.append_log(f"[失敗] {os.path.basename(path)} - 原因: {msg}")
            for item in self.file_list:
                if item['id'] == item_id:
                    item['status'] = '失敗'
        elif msg_type == "PROGRESS":
            processed, total = content
            self.status_var.set(f"轉檔進度: {processed} / {total}")
            if total > 0:
                self.progress_var.set((processed / total) * 100)
        elif msg_type == "ERROR":
            self.append_log(f"錯誤: {content}")
            messagebox.showerror("錯誤", content)
        elif msg_type == "PAUSE_REQUEST":
            self.append_log(f"發生 I/O 錯誤，暫停中: {content}")
            self.show_io_error_dialog(content)

    def show_io_error_dialog(self, msg):
        top = tk.Toplevel(self)
        top.title("⚠️ 儲存裝置異常中斷 ⚠️")
        top.geometry("800x400")
        top.attributes("-topmost", True)
        top.grab_set()

        btn_frame = ttk.Frame(top)
        btn_frame.pack(side=tk.BOTTOM, pady=20)

        lbl = ttk.Label(top, text=msg + "\n\n請將隨身碟插回，或排除問題後點擊繼續。", foreground="red", justify=tk.CENTER)
        lbl.pack(side=tk.TOP, expand=True, fill=tk.BOTH, padx=20, pady=20)

        def resume():
            pause_event.set()
            top.destroy()
        
        def abort():
            stop_event.set()
            pause_event.set()
            top.destroy()

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
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders") as key:
                desktop = winreg.QueryValueEx(key, "Desktop")[0]
                if '%USERPROFILE%' in desktop:
                    desktop = desktop.replace('%USERPROFILE%', os.environ.get('USERPROFILE', ''))
        except Exception:
            desktop = os.path.join(os.environ.get('USERPROFILE', ''), 'Desktop')
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
        retry_list = [item for item in self.file_list if item.get('status') == '失敗']
        
        self.file_list = retry_list
        for item in self.file_list:
            item['status'] = '等待重試'
            
        self.show_confirmation_dialog()

    def on_closing(self):
        if self.btn_start.instate(['disabled']): # 正在轉檔
            if not messagebox.askyesno("警告", "轉檔正在進行中，確定要強制關閉程式嗎？\n這將會中斷所有進度！"):
                return
            stop_event.set()
            pause_event.set()
        
        self.save_settings()
        self.destroy()

if __name__ == "__main__":
    app = AppUI()
    app.mainloop()
