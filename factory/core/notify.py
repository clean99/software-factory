import platform
import shutil
import subprocess


def send(message, title="OSS factory"):
    message = message.replace('"', "'")[:240]
    if platform.system() == "Darwin":
        script = f'display notification "{message}" with title "{title}" sound name "Glass"'
        subprocess.run(["osascript", "-e", script], check=False)
    elif shutil.which("notify-send"):
        subprocess.run(["notify-send", title, message], check=False)
    print(f"[notify] {title}: {message}")
