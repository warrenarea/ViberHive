import base64
import json
import os
import uuid

from . import windows


class Settings:
    def __init__(self, root):
        self.file = root / "settings.json"
        root.mkdir(parents=True, exist_ok=True)
        try:
            self.values = json.loads(self.file.read_text("utf-8"))
            if not isinstance(self.values, dict):
                self.values = {}
        except (ValueError, OSError):
            self.values = {}
        self.customer = self.values.setdefault("customer", uuid.uuid4().hex)
        self.values.setdefault("hotkey", "Ctrl+F12")
        self.values.setdefault("source", "Google Images")
        self.values.setdefault("cache_mib", 512)
        self.secrets = {}
        for name in ("serp", "klipy"):
            if windows.WINDOWS and self.values.get(name):
                try:
                    self.secrets[name] = windows.dpapi(
                        base64.b64decode(self.values[name]), True
                    ).decode()
                except (OSError, ValueError, UnicodeError):
                    pass
        self.save()

    def save(self):
        temp = self.file.with_suffix(".tmp")
        temp.write_text(json.dumps(self.values, indent=2), "utf-8")
        temp.replace(self.file)

    @property
    def serp(self):
        return os.getenv("SERPAPI_API_KEY", "").strip() or self.secrets.get("serp", "")

    @property
    def klipy(self):
        return os.getenv("KLIPY_APP_KEY", "").strip() or self.secrets.get("klipy", "")

    def set_key(self, name, value):
        self.secrets[name] = value
        if value and windows.WINDOWS:
            self.values[name] = base64.b64encode(windows.dpapi(value.encode())).decode()
        else:
            self.values.pop(name, None)
