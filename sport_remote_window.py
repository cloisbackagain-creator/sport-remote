#!/usr/bin/python3
"""Sport Remote in its own window (GTK 4 + WebKitGTK), instead of a browser tab.

Runs with the *system* Python, which has the GTK/WebKit bindings. It shows the UI served by the
bridge (sport_remote.py) on http://localhost:8766, forwards the page's notifications to the
desktop, and saves exported files (CSV, diagnostics) to ~/Downloads.
"""
import os
import sys

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("WebKit", "6.0")
from gi.repository import Gio, GLib, Gtk, WebKit  # noqa: E402

APP_ID = "app.sportremote.Window"
URL = "http://localhost:8766/"


class SportRemote(Gtk.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)

    def do_activate(self):
        # a second launch just raises the existing window
        if self.get_windows():
            self.get_windows()[0].present()
            return
        win = Gtk.ApplicationWindow(application=self, title="Sport Remote")
        win.set_default_size(1180, 780)

        view = WebKit.WebView()
        settings = view.get_settings()
        settings.set_enable_developer_extras("--debug" in sys.argv)
        view.connect("permission-request", self.on_permission)
        view.connect("show-notification", self.on_notification)
        view.connect("load-failed", self.on_load_failed)
        view.get_network_session().connect("download-started", self.on_download)
        view.load_uri(URL)

        # A bare WebView has no browser shortcuts; add reload. Capture phase so the page can't swallow them.
        keys = Gtk.ShortcutController()
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        for trigger, fn in (("F5", view.reload), ("<Control>r", view.reload),
                            ("<Control><Shift>r", view.reload_bypass_cache), ("<Shift>F5", view.reload_bypass_cache)):
            keys.add_shortcut(Gtk.Shortcut.new(Gtk.ShortcutTrigger.parse_string(trigger),
                                               Gtk.CallbackAction.new(lambda *_a, fn=fn: (fn(), True)[1])))
        win.add_controller(keys)

        # Closing the window quits the app (and with it, the page's link to the bridge),
        # even if WebKit is still holding something open.
        win.connect("close-request", lambda _w: (self.quit(), False)[1])

        win.set_child(view)
        win.present()

    # The page asks for notification permission from its Settings toggle; allow it here.
    def on_permission(self, _view, request):
        if isinstance(request, WebKit.NotificationPermissionRequest):
            request.allow()
            return True
        return False

    def on_notification(self, _view, notification):
        n = Gio.Notification.new(notification.get_title() or "Sport Remote")
        n.set_body(notification.get_body() or "")
        self.send_notification(notification.get_tag() or None, n)
        return True

    def on_download(self, _session, download):
        def decide(dl, suggested):
            folder = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_DOWNLOAD) or os.path.expanduser("~")
            dl.set_destination(os.path.join(folder, suggested or "sport-remote-export"))
            return True
        download.connect("decide-destination", decide)

    # The bridge may still be starting: retry for a few seconds before showing an error.
    def on_load_failed(self, view, _event, uri, _error):
        tries = getattr(self, "_tries", 0)
        if tries < 20:
            self._tries = tries + 1
            GLib.timeout_add(500, lambda: (view.load_uri(uri), False)[1])
            return True
        return False


if __name__ == "__main__":
    sys.exit(SportRemote().run([a for a in sys.argv if a != "--debug"]))
