# Protector.Net Access Control

**Version 0.1.2 – Add Configurable Entity Selection and Options Flow**

Custom Home Assistant integration to control Hartmann-controll Protector.Net door access control systems via their HTTP API.  
Supports:

- Cookie-based login with automatic session-ID refresh  
- Partition selection to only import the doors you care about  
- **Configurable entities**: pick exactly which button types to import (pulse, overrides, resume, timed unlock, card/PIN)  
- Button entities for each door:
  - Pulse Unlock
  - Resume Schedule
  - Unlock Until Resume
  - Unlock Until Next Schedule
  - CardOrPin Until Resume
  - Timed Override Unlock (with configurable default duration)

---

## Features

- **Config Flow**  
  Entirely through the Home Assistant UI—no YAML required.

- **Secure Login**  
  Prompts for your Protector.Net URL, username & password, and obtains the `ss-id` cookie used by the official web UI.

- **Auto Refresh**  
  Any time the panel returns `401 Unauthorized`, the integration automatically re-logs in under the hood so your buttons never stop working.

- **Partition Filtering**  
  If your site has multiple partitions, pick one during setup and only doors from that partition will be created.

- **Entity Selection**  
  Choose exactly which button types to import during setup—and revisit **Options** any time to add or remove types.

- **Button Entities**  
  Each door appears as independent buttons that you can wire into automations, scripts, dashboards, etc.

---

## Installation

1. **Download**  
   - Copy the `protector_net/` folder into your Home Assistant’s `config/custom_components/` directory.

2. **Restart Home Assistant**  
   - After the files are in place, restart HA so it picks up the new integration.

3. **Add Integration**  
   - In HA: **Settings → Devices & Services → Add Integration**  
   - Search for **Protector.Net Access Control** and follow the prompts.

---

## Configuration Steps

1. **Base URL**  
   Enter your panel’s URL, e.g. `https://doors.example.com:11001`.

2. **Username & Password**  
   Must be a Protector.Net user with **Use API** / **Manage_Doors** privileges.

3. **Default Override Minutes**  
   The duration used by the “Timed Override Unlock” button (default: 5).

4. **Partition Selection**  
   After successful login, choose one partition—only doors in this partition will be imported.

5. **Entity Selection**  
   Pick which button types you want (pulse, resume, timed, card/PIN, etc.). You can always revisit **Options** later to change these.

6. **Finish**  
   The integration will log in, fetch your doors, and create only the buttons you selected.

---

## Entities Created

For each door in your chosen partition and selected types, you’ll get:

| Entity Name                                  | Entity ID                                             | Action                                                         |
| -------------------------------------------- | ----------------------------------------------------- | -------------------------------------------------------------- |
| `<Door Name> Pulse Unlock`                   | `button.protector_net_<door>_pulse_unlock`            | Briefly pulses the door unlock relay                          |
| `<Door Name> Resume Schedule`                | `button.protector_net_<door>_resume_schedule`         | Cancels any override and returns to the normal schedule       |
| `<Door Name> Unlock Until Resume`            | `button.protector_net_<door>_unlock_until_resume`     | Overrides schedule to Unlock until manually resumed           |
| `<Door Name> Unlock Until Next Schedule`     | `button.protector_net_<door>_unlock_until_next_schedule` | Overrides to Unlock until the door’s next scheduled event      |
| `<Door Name> CardOrPin Until Resume`         | `button.protector_net_<door>_cardorpin_until_resume`  | Override until someone uses card or PIN                       |
| `<Door Name> Timed Override Unlock`          | `button.protector_net_<door>_timed_override_unlock`   | Override for the default minutes, then resume schedule        |

---

## Options

After setup, you can update:

- **Default Override Minutes**  
- **Entity Types to Import**

1. **Settings → Devices & Services**  
2. Click the **Protector.Net** integration  
3. Hit **Options**  
4. Change `override_minutes` or your selected `entities` and **Re-submit**

---

## Developer Notes

- **API Endpoints Used**  
  - Login: `POST /auth` → grabs `ss-id` cookie  
  - Doors: `GET /api/doors?PageNumber=1&PerPage=500&PartitionId=<id>`  
  - Commands:
    - `POST /api/PanelCommands/PulseDoor`
    - `POST /api/PanelCommands/OverrideDoor`
    - `POST /api/PanelCommands/ResumeDoor`

- **Automatic Re-authentication**  
  Wrapped in a helper that retries any request once after a 401.

---

## Changelog

### 0.1.2
- 🎉 **New:** Configurable entity selection during setup  
- 🔄 **New:** Options flow to add/remove entity types at any time  
- 🐛 Fixed: MRO init issue in button base class  
- ⚙️ Updated docs and readme

### 0.1.1
- Add partition selection  
- Automatic session-ID login & refresh  
- Dynamic integration title shows `host – partition name`

### 0.1.0
- Initial release: cookie-based login, door imports, basic button commands

---

> _By Yoel Goldstein / Vaayer LLC_  
