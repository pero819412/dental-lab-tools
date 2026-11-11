"""リネーム中のフォーカス喪失を「ユーザー操作」か「他アプリの自動前面化」か判定する（Windows 専用）。

痒いところ: 名前入力中に CAD などが勝手に前面へ出てきて入力欄が閉じる／確定してしまう事故を防ぐ。
物理的なマウス/修飾キーが押されていれば「ユーザーが自分で切り替えた」、押されておらず
既知の自動前面化アプリが前面なら「自動」と判定する。キー注入・入力フック・文字の取得は一切しない。

Classify rename deactivation without input hooks, key injection or text capture.

依存: psutil, pywin32（win32api / win32gui / win32process）。Windows 以外では常に 'unknown'。
"""
import os
from pathlib import Path

try:
    import psutil
except ImportError:  # 非 Windows での構文確認・import を妨げない
    psutil = None


# 自動前面化してくるアプリの判定ルール（自分の環境に合わせて設定する）。
#   name:  結果として返す理由文字列（'automatic-<name>'）
#   exe_names: 実行ファイル名（小文字）
#   path_contains: 実行ファイルのフルパス（小文字・\ 区切り）に含まれるべき文字列（すべて）
AUTOMATIC_PROCESS_RULES = [
    # 例: Java 製 CAD（同梱 JRE の javaw.exe で動くもの）
    # {"name": "cad", "exe_names": ("java.exe", "javaw.exe"),
    #  "path_contains": ("\\my_cad_app\\", "\\jre\\bin\\")},
]

# 自動前面化してくる自作 Python スクリプト（コマンドライン引数にこのパスがあれば自動扱い）。
#   {"name": "helper", "script": r"C:\path\to\helper_script.py"}
AUTOMATIC_SCRIPT_RULES = []

# 物理入力の判定に使う仮想キー: 左/右/中ボタン, X1/X2, Alt, 左右 Win
_USER_INPUT_KEYS = (0x01, 0x02, 0x04, 0x05, 0x06, 0x12, 0x5B, 0x5C)


def restore_rename_foreground(window_id):
    """Best-effort activation only; never inject keys or attach input threads."""
    try:
        import win32gui
        win32gui.SetForegroundWindow(int(window_id))
    except Exception:
        pass


def _matches_process_rule(executable, rule):
    exe_names = tuple(name.lower() for name in rule.get("exe_names", ()))
    if exe_names and Path(executable).name not in exe_names:
        return False
    return all(part.lower() in executable for part in rule.get("path_contains", ()))


def deactivation_reason(process_rules=None, script_rules=None):
    """'explicit-user' / 'automatic-<name>' / 'unknown' のいずれかを返す。"""
    if os.name != 'nt' or psutil is None:
        return 'unknown'
    process_rules = AUTOMATIC_PROCESS_RULES if process_rules is None else process_rules
    script_rules = AUTOMATIC_SCRIPT_RULES if script_rules is None else script_rules
    try:
        import win32api
        import win32gui
        import win32process
        # Read physical button/modifier state only. Never synthesize Alt/keys.
        if any(win32api.GetAsyncKeyState(key) & 0x8000 for key in _USER_INPUT_KEYS):
            return 'explicit-user'
        hwnd = win32gui.GetForegroundWindow()
        if not hwnd:
            return 'unknown'
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        process = psutil.Process(pid)
        executable = os.path.normcase(process.exe()).replace('/', '\\')
        for rule in process_rules:
            if _matches_process_rule(executable, rule):
                return 'automatic-' + str(rule.get("name", "process"))
        if script_rules:
            arguments = {os.path.normcase(os.path.abspath(arg)) for arg in process.cmdline()[1:]}
            for rule in script_rules:
                expected = os.path.normcase(os.path.abspath(str(rule.get("script", ""))))
                if rule.get("script") and expected in arguments:
                    return 'automatic-' + str(rule.get("name", "script"))
    except Exception:
        pass
    return 'unknown'
