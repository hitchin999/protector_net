# Protector.Net Access Control

**Version 0.1.4 – Action Plan Button Fix & Improvements**

Custom Home Assistant integration to control Hartmann-Controls Protector.Net door access systems via their HTTP API.  

---

## Features

- **Cookie-Based Login**  
  Securely logs in to Protector.Net, storing the `ss-id` cookie and automatically refreshing on `401 Unauthorized`.
  
- **Partition Selection**  
  If your site has multiple partitions, pick one during setup and only doors from that partition will be imported.

- **Configurable Entities**  
  During setup (and via Options), select exactly which door button types to import:
  - Pulse Unlock  
  - Resume Schedule  
  - Unlock Until Resume  
  - Unlock Until Next Schedule  
  - CardOrPin Until Resume  
  - Timed Override Unlock (with configurable default duration)

- **Action Plan Buttons**  
  Import your Protector.Net Action Plans as Home Assistant buttons, and run them on demand.  

- **Automatic Clone-and-Populate**  
  Trigger-type Action Plans are cloned into “System” plans named `"<Original Name> (Home Assistant)"`.  
  A two-step POST→PUT process ensures the full plan contents survive cloning.

- **Home Assistant Door-Log**  
  Every time you press a door-button in HA, the integration logs  
  ```
  Home Assistant unlocked <Door Name>
  ```
  in your Protector.Net panel’s system log.

- **Fully UI-Driven**  
  No YAML needed—complete setup and options flow in the Home Assistant UI.

---

## Installation

1. **Copy**  
   Place the `protector_net/` folder into your HA config under `custom_components/`.
2. **Restart**  
   Restart Home Assistant.
3. **Add Integration**  
   In the UI: **Settings → Devices & Services → Add Integration → Protector.Net Access Control**  
   Follow the prompts.

---

## Configuration Steps

1. **Base URL**  
   `https://your-panel-host:port`

2. **Credentials**  
   - Username & Password must have **System Admin** privileges in Protector.Net.

3. **Default Override Minutes**  
   Used by the “Timed Override Unlock” button (default: 5 minutes).

4. **Partition Selection**  
   Choose one partition to import.

5. **Entity Selection**  
   Select which door-control buttons you want.

6. **Action Plan Selection**  
   Pick which Trigger-type plans to import as buttons. These will be cloned internally into System-type plans you can run on demand.

7. **Finish**  
   Integration creates the buttons; you can immediately use them.

---

## Entities Created

### Door Buttons

| Entity Name                                  | Entity ID                                            | Action                                                         |
|----------------------------------------------|------------------------------------------------------|----------------------------------------------------------------|
| `<Door> Pulse Unlock`                        | `button.protector_net_<host>_<door>_pulse_unlock`    | Pulse unlock relay                                             |
| `<Door> Resume Schedule`                     | `button.protector_net_<host>_<door>_resume_schedule` | Resume normal schedule                                         |
| `<Door> Unlock Until Resume`                 | `button.protector_net_<host>_<door>_unlock_until_resume` | Unlock until manually resumed                              |
| `<Door> Unlock Until Next Schedule`          | `button.protector_net_<host>_<door>_unlock_until_next_schedule` | Unlock until next event                               |
| `<Door> CardOrPin Until Resume`              | `button.protector_net_<host>_<door>_cardorpin_until_resume` | Unlock until card/PIN                                   |
| `<Door> Timed Override Unlock`               | `button.protector_net_<host>_<door>_timed_override_unlock` | Unlock for default minutes then resume schedule        |

### Action Plan Buttons

| Entity Name                     | Entity ID                                        | Action                                         |
|---------------------------------|---------------------------------------------------|------------------------------------------------|
| `Action Plan: <Plan Name>`      | `button.protector_net_<host>_action_plan_<id>`    | Executes the cloned System-type plan via API   |

---

## Options Flow

After setup, you can adjust:

- **Default Override Minutes**  
- **Door Entity Types**  
- **Action Plans to Import**

**Path**: Settings → Devices & Services → Protector.Net → Options

---

## Developer Notes

### API Endpoints

- **Login**: `POST /auth` → returns `ss-id` cookie  
- **Partitions**: `GET /api/Partitions/ByPrivilege/Manage_Doors`  
- **Doors**: `GET /api/doors?PartitionId=<id>&PageNumber=1&PerPage=500`  
- **Action Plans**:  
  - `GET /api/ActionPlans?PartitionId=<id>&PageNumber=1&PerPage=500`  
  - `POST /api/ActionPlans` (create System plan)  
  - `PUT /api/ActionPlans/{Id}` (update Contents)  
  - `POST /api/ActionPlans/{Id}/Exec/{LogLevel}?PartitionId=<id>` (execute)

### Clone-and-Populate Workflow

1. **find_or_clone_system_plan()**  
   - Checks for an existing System-type plan named `"<Trigger Name> (Home Assistant)"`.  
   - If none exists:
     1. **POST** skeleton System plan (no Contents)  
     2. **PUT** the original plan’s `Contents` JSON back into it  
   - Returns the System plan’s ID.

2. **find_or_create_ha_log_plan()**  
   - Ensures a single System plan named **HA Door Log** exists.  
   - Populates its Contents once, so that **every** door-button press logs  
     ```
     Home Assistant unlocked <Door Name>
     ```  
     Plus a follow-up line `<Door Name> logged`.

3. **execute_action_plan()**  
   Issues a `POST /Exec/Info` with optional `{ "SessionVars": {...} }` body.

---

## Changelog

### 0.1.5
- 🆕 **New:** “Home Assistant unlocked…” log entries in Protector.Net panel for every door-button press

### 0.1.4
- 🐛 **Fixed:** Action Plan Buttons now clone & populate plan contents via two-step POST→PUT  
- 🔄 **Improved:** `find_or_clone_system_plan` reuses existing clones; no duplicates on reconfigure  
- 🐛 **Fixed:** Empty Action Plan clones  
- 🆕 **New:** “Home Assistant unlocked…” log entries in Protector.Net panel for every door-button press

### 0.1.3
- 🎉 **New:** Import & execute Protector.Net Action Plans as buttons  
- 🔄 **Improved:** Options flow always refreshes available plans  
- 🐛 **Fixed:** Entity uniqueness and “unavailable” plan errors  

### 0.1.2
- 🎉 **New:** Configurable door entity selection  
- 🔄 **New:** Options flow for entity types  
- 🐛 **Fixed:** MRO init issue in Button base class  
- ⚙️ **Docs:** README updates  

### 0.1.1
- Partition selection  
- Automatic session-ID refresh  
- Dynamic integration title  

### 0.1.0
- Initial release: door imports & basic button commands  

> _By Yoel Goldstein / Vaayer LLC_
