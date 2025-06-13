# Protector.Net Access Control

**Version 0.1.1**

Custom Home Assistant integration to control Hartmann-controlls Protector.Net door access control systems via their HTTP API.  
Supports:

- Cookie-based login with automatic session-ID refresh  
- Partition selection to only import the doors you care about  
- Button entities for each door:
  - Pulse Unlock
  - Resume Schedule
  - Override Unlock Until Resume
  - Override Unlock Until Next Schedule
  - Override Until Resume (CardOrPin)
  - Timed Override Unlock (with configurable default duration)

---

## Features

- **Config Flow**  
  Set up entirely through the Home Assistant UI—no YAML required.

- **Secure Login**  
  Prompts for your Protector.Net URL, username & password, and obtains the `ss-id` cookie used by the official web UI.

- **Auto Refresh**  
  Any time the panel returns `401 Unauthorized`, the integration automatically re-logs in under the hood so your buttons never stop working.

- **Partition Filtering**  
  If your site has multiple partitions, pick one during setup and only doors from that partition will be created.

- **Button Entities**  
  Each door appears as six independent buttons. You can wire these into automations, scripts, dashboards, etc.

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

5. **Finish**  
   The integration will log in, fetch your doors, and create button entities for each.

---

## Entities Created

For each door in your chosen partition, you’ll get:

| Entity Name                                  | Entity ID                             | Action                                                         |
| -------------------------------------------- | ------------------------------------- | -------------------------------------------------------------- |
| `<Door Name> Pulse Unlock`                   | `button.protector_net_<door>_pulse`   | Briefly pulses the door unlock relay                          |
| `<Door Name> Resume Schedule`                | `button.protector_net_<door>_resume`  | Cancels any override and returns to the normal schedule       |
| `<Door Name> Override Until Resume`          | `button.protector_net_<door>_until_resume` | Overrides schedule until manually resumed                 |
| `<Door Name> Override Until Next Schedule`   | `button.protector_net_<door>_until_next_schedule` | Overrides until the panel’s next scheduled event      |
| `<Door Name> Override Until Resume (CardOrPin)` | `button.protector_net_<door>_until_resume_card_or_pin` | Override until someone uses card or PIN  |
| `<Door Name> Timed Override Unlock`          | `button.protector_net_<door>_timed_override` | Override for the default minutes, then resume schedule    |

---

## Options

After setup, you can update **Default Override Minutes** at any time:

1. **Settings → Devices & Services**  
2. Click the **Protector.Net** integration  
3. Hit **Options**  
4. Change **override_minutes** and **Re-submit**

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

### 0.1.1
- Add partition selection  
- Automatic session-ID login & refresh  
- Dynamic integration title shows `host – partition name`  

### 0.1.0
- Initial release: cookie-based login, door imports, basic button commands  

---

> _By Yoel Goldstein / Vaayer LLC_
