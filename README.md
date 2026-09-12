# TLPedit

A GTK 4, Wayland-native editor for TLP on Fedora. It discovers settings from the installed TLP configuration, groups them by category, offers safe controls for known values, and preserves untouched configuration comments.

## Supported systems

Fedora 44 and newer with GTK 4, Python 3, TLP, and PolicyKit installed. The application automatically detects the usual Fedora paths for `tlp` and `tlp-stat`.

## Requirements

- `python3-gobject`
- `gtk4`
- `tlp`
- `polkit`

## Run

```bash
python3 tlpedit.py
```

## Safety behavior

- Checks a candidate configuration before requesting administrator authorization.
- Blocks saving when validation finds an error.
- Shows a copyable validation log.
- Writes with a private temporary file and creates a numbered backup of `/etc/tlp.conf`.
- Reports clearly if saving succeeds but applying TLP fails.

This repository contains no personal paths, hardware inventory, or copied local TLP settings.
