"""
Monobridge.app - menu-bar wrapper around monobridge.py.

Runs the bridge in the background, shows connection status in the menu bar,
and opens the control panel in the browser. Built into a .app by build_app.sh.
"""
import os
import subprocess
import sys
import threading
import webbrowser

import rumps

import monobridge as mb

APP_NAME = "Monobridge"
SUPPORT = os.path.expanduser("~/Library/Application Support/Monobridge")
CONFIG = os.path.join(SUPPORT, "mapping.json")
LOG = os.path.expanduser("~/Library/Logs/Monobridge.log")
AGENT = os.path.expanduser("~/Library/LaunchAgents/org.monobridge.app.plist")
RES = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
ICON_ON = os.path.join(RES, "menubar_on.png")
ICON_OFF = os.path.join(RES, "menubar_off.png")
ACCESSIBILITY_URL = "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"


def app_bundle_path():
    # .../Monobridge.app/Contents/MacOS/Monobridge -> .../Monobridge.app
    p = os.path.abspath(sys.executable)
    for _ in range(3):
        p = os.path.dirname(p)
    return p if p.endswith(".app") else None


def login_item_enabled():
    return os.path.exists(AGENT)


def set_login_item(on):
    if not on:
        if os.path.exists(AGENT):
            os.remove(AGENT)
        return True
    path = app_bundle_path()
    if not path:
        return False
    os.makedirs(os.path.dirname(AGENT), exist_ok=True)
    with open(AGENT, "w") as f:
        f.write(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>org.monobridge.app</string>
  <key>ProgramArguments</key><array><string>/usr/bin/open</string><string>-a</string><string>{path}</string></array>
  <key>RunAtLoad</key><true/>
</dict></plist>
""")
    return True


class MonobridgeApp(rumps.App):
    def __init__(self):
        if os.path.exists(ICON_OFF):
            super().__init__(APP_NAME, icon=ICON_OFF, template=True, quit_button=None)
            self._icons = True
        else:                                   # running from source without icons
            super().__init__(APP_NAME, title="○", quit_button=None)
            self._icons = False
        self._shown = None
        self.status_item = rumps.MenuItem("Starting…")
        self.login_item = rumps.MenuItem("Start at Login", callback=self.toggle_login)
        self.login_item.state = login_item_enabled()
        self.keys_item = rumps.MenuItem("Allow Keystrokes & Scrolling…", callback=self.open_accessibility)
        self.menu = [
            self.status_item,
            None,
            rumps.MenuItem("Open Control Panel", callback=self.open_panel),
            rumps.MenuItem("Import Monogram Profile…", callback=self.import_profile),
            self.keys_item,
            self.login_item,
            None,
            rumps.MenuItem("Show Log", callback=self.show_log),
            rumps.MenuItem("Quit Monobridge", callback=self.quit_app),
        ]
        self.service = mb.Service(CONFIG)
        threading.Thread(target=self.service.run_forever, daemon=True).start()
        self.timer = rumps.Timer(self.refresh, 1)
        self.timer.start()
        if not os.path.exists(CONFIG):
            # First run: show the panel so people find it
            threading.Timer(2, lambda: webbrowser.open(self.service.url)).start()

    def refresh(self, _=None):
        s = self.service
        if s.connected != self._shown:
            self._shown = s.connected
            if self._icons:
                self.icon = ICON_ON if s.connected else ICON_OFF
            else:
                self.title = "●" if s.connected else "○"
        n = len(s.bridge.modules)
        self.status_item.title = f"Connected · {n} modules" if s.connected else s.status
        try:
            if not hasattr(self, "_mac"):
                self._mac = mb.MacEvents()
            trusted = self._mac.trusted()
        except Exception:
            trusted = False
        self.keys_item.title = ("Keystrokes & Scrolling: allowed ✓" if trusted
                                else "Allow Keystrokes & Scrolling…")

    def open_panel(self, _):
        webbrowser.open(self.service.url)

    def import_profile(self, _):
        script = ('POSIX path of (choose file with prompt '
                  '"Choose a Monogram Creator profile (.monogram) to import")')
        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
        path = r.stdout.strip()
        if r.returncode != 0 or not path:
            return                                   # cancelled
        try:
            prof = mb.Profile(path)
        except Exception as e:
            rumps.alert("Import failed", f"That file doesn't look like a Monogram profile.\n\n{e}")
            return
        b = self.service.bridge
        b.profile = prof
        b.map["profile"] = None                      # apply it fully, replacing current settings
        b.map.pop("screen", None)
        b._screen_done = False
        if b.core:
            b.core.send({"get_layout": True})        # re-applies MIDI, colours and display
        rumps.notification(APP_NAME, "Profile imported", f"“{prof.title}” is now active.")

    def open_accessibility(self, _):
        subprocess.run(["open", ACCESSIBILITY_URL])
        rumps.alert(
            "Allow keystrokes & scrolling",
            "In the list that opens, switch on Monobridge (use + to add it from your "
            "Applications folder if it isn't there).\n\nThen quit and reopen Monobridge.")

    def toggle_login(self, item):
        want = not item.state
        if set_login_item(want):
            item.state = want
        else:
            rumps.alert("Start at Login", "Move Monobridge to your Applications folder first.")

    def show_log(self, _):
        subprocess.run(["open", "-a", "Console", LOG])

    def quit_app(self, _):
        self.service.stop()
        self.service.save()
        rumps.quit_application()


def main():
    os.makedirs(SUPPORT, exist_ok=True)
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    log = open(LOG, "a", buffering=1)
    sys.stdout = sys.stderr = log
    print(f"--- {APP_NAME} starting ---")
    MonobridgeApp().run()


if __name__ == "__main__":
    main()
