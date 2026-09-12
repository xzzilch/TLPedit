#!/usr/bin/env python3
"""A small GTK 4 / Wayland-native editor for Fedora's TLP configuration.

The editor deliberately keeps the original file layout and comments. Settings
that are changed in the UI are uncommented; untouched lines remain byte-for-
byte unchanged. Saving is performed through pkexec because /etc/tlp.conf is
normally owned by root.
"""

import os
import re
import subprocess
import tempfile
from dataclasses import dataclass

import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, Gio, GLib, Gdk


CONFIG = "/etc/tlp.conf"
DEFAULTS = "/usr/share/tlp/defaults.conf"
TLP_BIN = next((path for path in ("/usr/sbin/tlp", "/usr/bin/tlp") if os.path.exists(path)), "tlp")
TLP_STAT_BIN = next((path for path in ("/usr/sbin/tlp-stat", "/usr/bin/tlp-stat") if os.path.exists(path)), "tlp-stat")
ASSIGNMENT = re.compile(
    r"^(?P<indent>\s*)(?P<comment>#\s*)?(?P<key>[A-Z][A-Z0-9_]*)"
    r"\s*=\s*(?P<value>.*?)(?P<newline>\n?)$"
)

CATEGORY_ORDER = [
    "General",
    "CPU and platform",
    "Disk and storage",
    "Graphics",
    "Network and wireless",
    "USB and devices",
    "Battery and charging",
    "Profiles",
    "Other",
]


def category_for(key):
    if key.startswith(("CPU_", "NMI_", "PLATFORM_")):
        return "CPU and platform"
    if key.startswith(("DISK_", "SATA_", "AHCI_")):
        return "Disk and storage"
    if key.startswith(("INTEL_GPU_", "RADEON_", "AMDGPU_")):
        return "Graphics"
    if key.startswith(("WIFI_", "WOL_")):
        return "Network and wireless"
    if key.startswith(("USB_", "RUNTIME_PM", "BAY_", "SOUND_")):
        return "USB and devices"
    if key.startswith(("BAT", "RESTORE_", "NATACPI", "TPSMAPI")):
        return "Battery and charging"
    if key.startswith(("TLP_", "DEVICES_")):
        return "General"
    if key.endswith(("_AC", "_BAT", "_SAV")):
        return "Profiles"
    return "Other"


@dataclass
class Setting:
    key: str
    value: str
    original: str
    active: bool
    line_index: int


def read_source(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.readlines()
    except OSError:
        return []


def discover(lines):
    settings = []
    seen = set()
    for index, line in enumerate(lines):
        match = ASSIGNMENT.match(line)
        if not match:
            continue
        key = match.group("key")
        if key in seen:
            continue
        seen.add(key)
        value = match.group("value").strip()
        settings.append(Setting(key, value, value, not bool(match.group("comment")), index))
    return settings


class TlpEditor(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="io.github.tlpgtkeditor", flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.lines = []
        self.settings = []
        self.widgets = {}
        self.changed = False
        self.allow_close = False
        self.connect("activate", self.on_activate)

    def on_activate(self, app):
        if hasattr(self, "window"):
            self.window.present()
            return
        self.load_config()
        self.build_window()

    def load_config(self):
        self.lines = read_source(CONFIG) or read_source(DEFAULTS)
        if not self.lines:
            raise RuntimeError("TLP configuration files were not found. Install the tlp package first.")
        existing = {item.key for item in discover(self.lines)}
        for item in discover(read_source(DEFAULTS)):
            if item.key not in existing:
                self.lines.append(f"# {item.key}={item.value}\n")
                existing.add(item.key)
        self.settings = discover(self.lines)
        self.settings.sort(key=lambda item: (CATEGORY_ORDER.index(category_for(item.key)), item.key))

    def build_window(self):
        self.window = Gtk.ApplicationWindow(application=self)
        self.window.set_title("TLP Settings")
        css = Gtk.CssProvider()
        css.load_from_data(b"window { font-family: Adwaita Sans, sans-serif; font-size: 14px; } headerbar { padding: 6px 10px; } entry, dropdown { min-height: 36px; } .boxed-list { border-radius: 12px; } .title-4 { margin-left: 10px; font-weight: 700; } button { min-height: 34px; padding-left: 14px; padding-right: 14px; }")
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self.window.set_default_size(920, 720)
        self.window.connect("close-request", self.on_close)

        header = Gtk.HeaderBar()
        self.window.set_titlebar(header)
        self.status = Gtk.Label(label=f"{len(self.settings)} settings discovered")
        self.status.add_css_class("dim-label")
        header.pack_start(self.status)

        check_button = Gtk.Button(label="Check configuration")
        check_button.set_tooltip_text("Validate pending changes and show a copyable diagnostic log")
        check_button.connect("clicked", self.on_check)
        header.pack_end(check_button)
        reload_button = Gtk.Button(label="Reload")
        reload_button.set_tooltip_text("Discard unsaved changes and reload /etc/tlp.conf")
        reload_button.connect("clicked", self.on_reload)
        header.pack_end(reload_button)
        self.save_button = Gtk.Button(label="Save and apply")
        self.save_button.add_css_class("suggested-action")
        self.save_button.connect("clicked", self.on_save)
        header.pack_end(self.save_button)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        root.set_margin_top(20); root.set_margin_bottom(20); root.set_margin_start(24); root.set_margin_end(24)
        self.window.set_child(root)

        info = Gtk.Label(label="Edit any TLP option. Blank values disable an option; values are validated by TLP when applied.", xalign=0)
        info.add_css_class("dim-label")
        root.append(info)

        self.category_filter = Gtk.DropDown(model=Gtk.StringList.new(["All categories"] + CATEGORY_ORDER))
        self.category_filter.set_selected(0)
        self.category_filter.connect("notify::selected", lambda *_: self.rebuild_rows())
        filters = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.category_filter.set_size_request(230, -1)
        filters.append(self.category_filter)

        self.search = Gtk.SearchEntry(placeholder_text="Search settings…")
        self.search.connect("search-changed", lambda *_: self.rebuild_rows())
        self.search.set_hexpand(True)
        filters.append(self.search)
        root.append(filters)

        self.summary = Gtk.Label(xalign=0, wrap=True)
        self.summary.add_css_class("dim-label")
        root.append(self.summary)

        scroller = Gtk.ScrolledWindow(vexpand=True, hexpand=True)
        self.listbox = Gtk.ListBox()
        self.listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        self.listbox.add_css_class("boxed-list")
        scroller.set_child(self.listbox)
        root.append(scroller)

        self.rebuild_rows()
        self.window.present()

    def rebuild_rows(self):
        while child := self.listbox.get_first_child():
            self.listbox.remove(child)
        query = self.search.get_text().strip().lower() if hasattr(self, "search") else ""
        selected_item = self.category_filter.get_selected_item() if hasattr(self, "category_filter") else None
        selected_category = selected_item.get_string() if selected_item else "All categories"
        shown = 0
        visible_categories = set()
        for setting in self.settings:
            if selected_category != "All categories" and category_for(setting.key) != selected_category:
                continue
            if query and query not in setting.key.lower() and query not in setting.value.lower():
                continue
            category = category_for(setting.key)
            if category not in visible_categories:
                heading = Gtk.Label(label=category, xalign=0)
                heading.add_css_class("title-4")
                heading.set_margin_top(14); heading.set_margin_bottom(4)
                self.listbox.append(heading)
                visible_categories.add(category)
            row = Gtk.ListBoxRow()
            box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            box.set_margin_top(8); box.set_margin_bottom(8); box.set_margin_start(12); box.set_margin_end(12)
            labels = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
            title = Gtk.Label(label=setting.key, xalign=0)
            title.add_css_class("heading")
            hint = Gtk.Label(label=f"{category_for(setting.key)} · {"Active" if setting.active else "Commented default"}", xalign=0)
            hint.add_css_class("dim-label")
            guidance = Gtk.Label(label=value_hint(setting.key), xalign=0, wrap=True)
            guidance.add_css_class("dim-label")
            labels.append(title); labels.append(hint); labels.append(guidance)
            choices = choices_for(setting.key)
            if choices:
                options = ["(disabled)"] + choices
                if setting.value and setting.value not in choices: options.insert(1, setting.value)
                entry = Gtk.DropDown(model=Gtk.StringList.new(options), hexpand=True)
                entry.set_tooltip_text(f"Select a value for {setting.key}")
                entry.set_selected(options.index(setting.value) if setting.value in options else 0)
                entry.connect("notify::selected", self.on_dropdown_changed, setting)
            else:
                entry = Gtk.Entry(hexpand=True)
                entry.set_text(setting.value)
                entry.set_placeholder_text("(disabled)")
                entry.set_tooltip_text(f"TLP option {setting.key}")
                entry.connect("changed", self.on_entry_changed, setting)
            self.widgets[setting.key] = entry
            box.append(labels); box.append(entry)
            row.set_child(box); self.listbox.append(row); shown += 1
        self.status.set_text(f"{shown} shown · {len(self.settings)} total settings")
        counts = {category: sum(category_for(item.key) == category for item in self.settings) for category in CATEGORY_ORDER}
        self.summary.set_text("  ·  ".join(f"{category}: {count}" for category, count in counts.items() if count))

    def on_dropdown_changed(self, dropdown, _param, setting):
        item = dropdown.get_selected_item()
        value = "" if not item or item.get_string() == "(disabled)" else item.get_string()
        self.set_setting_value(value, setting)

    def set_setting_value(self, value, setting):
        if value != setting.value:
            setting.value = value
            self.changed = True
            self.save_button.set_label("Save and apply *")

    def on_entry_changed(self, entry, setting):
        self.set_setting_value(entry.get_text().strip(), setting)

    def on_reload(self, _button):
        if not self.changed:
            self.reload_now()
            return
        dialog = Gtk.AlertDialog(message="Discard unsaved changes?", detail="Reloading will restore the file from disk.")
        dialog.set_buttons(["Cancel", "Discard"]); dialog.choose(self.window, None, self.on_reload_choice)

    def on_reload_choice(self, dialog, result):
        try:
            if dialog.choose_finish(result) == 1: self.reload_now()
        except GLib.Error:
            pass

    def reload_now(self):
        self.load_config(); self.changed = False; self.widgets = {}
        self.save_button.set_label("Save and apply"); self.rebuild_rows()

    def build_candidate(self):
        candidate = list(self.lines)
        for setting in self.settings:
            if setting.value == setting.original:
                continue
            old = candidate[setting.line_index]
            newline = "\n" if old.endswith("\n") else ""
            candidate[setting.line_index] = f"{setting.key}={setting.value}{newline}" if setting.value else f"# {setting.key}={newline}"
        return candidate

    def validate_candidate(self, candidate):
        for line in candidate:
            if "\x00" in line:
                raise ValueError("Configuration cannot contain null characters")
        for setting in self.settings:
            if ("START_CHARGE_THRESH" in setting.key or "STOP_CHARGE_THRESH" in setting.key) and setting.value:
                if not setting.value.isdigit() or not 0 <= int(setting.value) <= 100:
                    raise ValueError(f"{setting.key} must be a whole number from 0 to 100")

    def preflight(self, candidate):
        errors = []
        log = ["TLP GTK Editor — pre-save validation", "=" * 40, f"Settings checked: {len(self.settings)}"]
        try:
            self.validate_candidate(candidate)
            log.append("Candidate structure and supported numeric ranges: passed")
        except ValueError as error:
            errors.append(str(error))
            log.append(f"ERROR: {error}")
        active_keys = {}
        for number, line in enumerate(candidate, start=1):
            match = ASSIGNMENT.match(line)
            stripped = line.strip()
            if match and not match.group("comment"):
                key = match.group("key")
                if key in active_keys:
                    message = f"Duplicate active setting {key} on lines {active_keys[key]} and {number}"
                    errors.append(message); log.append(f"ERROR: {message}")
                active_keys[key] = number
            elif stripped and not stripped.startswith("#") and "=" in stripped:
                message = f"Unrecognized active configuration syntax on line {number}"
                errors.append(message); log.append(f"ERROR: {message}")
        if not errors:
            log.append("Active-setting uniqueness and line syntax: passed")
        try:
            diagnostic = subprocess.run([TLP_STAT_BIN, "-w"], capture_output=True, text=True)
            diagnostic_text = (diagnostic.stdout + diagnostic.stderr).strip()
            log.extend(["", "Current TLP diagnostics (installed configuration):", diagnostic_text or "No diagnostic output."])
            if diagnostic.returncode != 0:
                message = "TLP warning check returned a non-zero status"
                errors.append(message); log.append(f"ERROR: {message}")
        except OSError as error:
            message = f"Could not run tlp-stat: {error}"
            errors.append(message); log.extend(["", f"ERROR: {message}"])
        log.extend(["", "Result: " + ("FAILED — nothing was written." if errors else "PASSED — safe to request save authorization.")])
        return errors, "\n".join(log)

    def on_check(self, _button):
        candidate = self.build_candidate()
        errors, log = self.preflight(candidate)
        self.show_log("Configuration check", log, bool(errors))


    def on_save(self, _button):
        if not self.changed:
            self.show_message("No changes", "There are no edits to save.")
            return
        candidate = self.build_candidate()
        errors, log = self.preflight(candidate)
        if errors:
            self.show_log("Validation failed — no changes made", log, True)
            return
        temp_path = None
        try:
            fd, temp_path = tempfile.mkstemp(prefix="tlp-gtk-", text=True)
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.writelines(candidate)
            command = ["pkexec", "/usr/bin/install", "--backup=numbered", "-o", "root", "-g", "root", "-m", "0644", temp_path, CONFIG]
            result = subprocess.run(command, capture_output=True, text=True)
            if result.returncode != 0:
                raise RuntimeError(result.stderr.strip() or "Authorization was cancelled")
        except (OSError, RuntimeError, ValueError) as error:
            self.show_message("Could not save", str(error), error=True)
            return
        finally:
            if temp_path:
                try: os.unlink(temp_path)
                except FileNotFoundError: pass
        self.lines = candidate
        for setting in self.settings:
            setting.original = setting.value
            setting.active = bool(setting.value)
        self.changed = False
        self.save_button.set_label("Save and apply")
        apply_result = subprocess.run(["pkexec", TLP_BIN, "start"], capture_output=True, text=True)
        if apply_result.returncode != 0:
            self.show_message("Saved, but not applied", apply_result.stderr.strip() or "Run tlp start manually to apply the saved file.", error=True)
            return
        self.show_message("Saved and applied", "A versioned backup of the prior TLP configuration was kept automatically.")

    def copy_log(self, log, button):
        display = Gdk.Display.get_default()
        if display:
            provider = Gdk.ContentProvider.new_for_bytes("text/plain;charset=utf-8", GLib.Bytes.new(log.encode()))
            display.get_clipboard().set_content(provider)
            button.set_label("Copied")

    def show_log(self, title, log, error=False):
        dialog = Gtk.Window(title=title, transient_for=self.window, modal=True)
        dialog.set_default_size(760, 520)
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        root.set_margin_top(18); root.set_margin_bottom(18); root.set_margin_start(20); root.set_margin_end(20)
        dialog.set_child(root)
        heading = Gtk.Label(label="Validation failed — no changes were made" if error else "Configuration check passed", xalign=0)
        heading.add_css_class("title-3")
        root.append(heading)
        text = Gtk.TextView(editable=False, cursor_visible=False, monospace=True, wrap_mode=Gtk.WrapMode.WORD_CHAR)
        text.get_buffer().set_text(log)
        scroller = Gtk.ScrolledWindow(vexpand=True, hexpand=True)
        scroller.set_child(text); root.append(scroller)
        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, halign=Gtk.Align.END)
        copy_button = Gtk.Button(label="Copy log")
        copy_button.connect("clicked", lambda button: self.copy_log(log, button))
        close_button = Gtk.Button(label="Close")
        close_button.connect("clicked", lambda *_: dialog.close())
        buttons.append(copy_button); buttons.append(close_button); root.append(buttons)
        dialog.present()


    def show_message(self, title, text, error=False):
        dialog = Gtk.MessageDialog(transient_for=self.window, modal=True, text=title,
                                   secondary_text=text, buttons=Gtk.ButtonsType.OK)
        if error: dialog.add_css_class("error")
        dialog.connect("response", lambda d, _: d.close())
        dialog.present()

    def on_close(self, _window):
        if self.changed and not self.allow_close:
            dialog = Gtk.AlertDialog(message="Unsaved changes", detail="Save and apply before closing, or discard your edits.")
            dialog.set_buttons(["Cancel", "Discard"]); dialog.choose(self.window, None, self.on_close_choice)
            return True
        return False

    def on_close_choice(self, dialog, result):
        try:
            if dialog.choose_finish(result) == 1:
                self.allow_close = True
                self.window.close()
        except GLib.Error:
            pass


def sysfs_choices(path):
    try:
        with open(path, encoding="utf-8") as source:
            return source.read().split()
    except OSError:
        return []


def choices_for(key):
    binary = {
        "TLP_ENABLE",
        "NMI_WATCHDOG",
        "USB_AUTOSUSPEND",
        "USB_EXCLUDE_AUDIO",
        "USB_EXCLUDE_BTUSB",
        "USB_EXCLUDE_PHONE",
        "USB_EXCLUDE_PRINTER",
        "USB_EXCLUDE_WWAN",
        "RESTORE_DEVICE_STATE_ON_STARTUP",
        "RESTORE_THRESHOLDS_ON_BAT",
        "SOUND_POWER_SAVE_CONTROLLER",
        "WOL_DISABLE",
    }
    if key in binary:
        return ["0", "1"]
    if key == "TLP_AUTO_SWITCH":
        return ["0", "1", "2"]
    if key == "TLP_WARN_LEVEL":
        return [str(index) for index in range(4)]
    if "POLICY" in key:
        return ["performance", "balance_performance", "balance_power", "power"]
    if "PLATFORM_PROFILE" in key:
        return sysfs_choices("/sys/firmware/acpi/platform_profile_choices") or None
    if "POWER_PROFILE" in key:
        return ["balanced", "performance", "power_saving"]
    if key in {"AHCI_RUNTIME_PM_ON_AC", "AHCI_RUNTIME_PM_ON_BAT", "RUNTIME_PM_ON_AC", "RUNTIME_PM_ON_BAT"}:
        return ["on", "off", "auto"]
    if key.startswith("WIFI_PWR_"):
        return ["on", "off"]
    # SATA link power accepts per-device lists, so it remains a text input.
    if key.startswith("PCIE_ASPM_"):
        return ["default", "performance", "powersave", "powersupersave"]
    if "SCALING_GOVERNOR" in key:
        return sysfs_choices("/sys/devices/system/cpu/cpu0/cpufreq/scaling_available_governors") or None
    if "CPU_BOOST" in key:
        return ["0", "1"]
    if "RADEON_DPM_PERF_LEVEL" in key:
        return ["auto", "low", "high", "manual"]
    return None


def value_hint(key):
    binary = {
        "TLP_ENABLE", "TLP_AUTO_SWITCH", "NMI_WATCHDOG", "USB_AUTOSUSPEND",
        "USB_EXCLUDE_AUDIO", "USB_EXCLUDE_BTUSB", "USB_EXCLUDE_PHONE",
        "USB_EXCLUDE_PRINTER", "USB_EXCLUDE_WWAN",
        "RESTORE_DEVICE_STATE_ON_STARTUP", "RESTORE_THRESHOLDS_ON_BAT",
        "SOUND_POWER_SAVE_CONTROLLER", "WOL_DISABLE",
    }
    if key in binary:
        return "Allowed: 0 or 1"
    if key == "TLP_WARN_LEVEL":
        return "Allowed: 0–3"
    if "STOP_CHARGE_THRESH" in key or "START_CHARGE_THRESH" in key:
        return "Allowed: integer percentage 0–100"
    if "POLICY" in key:
        return "Typical: performance, balance_performance, balance_power, power"
    if "PLATFORM_PROFILE" in key:
        return "Typical: performance, balanced, low-power (hardware-dependent)"
    if "POWER_PROFILE" in key:
        return "Typical: balanced, performance, power_saving (hardware-dependent)"
    if "_PM_" in key or key.startswith("AHCI_RUNTIME_PM") or key.startswith("RUNTIME_PM_ON"):
        return "Allowed: on, off, auto"
    if key.startswith("WIFI_PWR_"):
        return "Allowed: on or off"
    if any(word in key for word in ("TIMEOUT", "SECS", "LEVEL", "THRESH")):
        return "Allowed: integer (see TLP documentation)"
    if any(word in key for word in ("DEVICES", "DENYLIST", "ENABLE", "DISABLE")):
        return "Allowed: space-separated device/driver names"
    return "Allowed values depend on hardware; see TLP documentation"

if __name__ == "__main__":
    TlpEditor().run(None)
