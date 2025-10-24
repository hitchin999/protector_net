
[![Total Downloads](https://img.shields.io/github/downloads/hitchin999/protector_net/total.svg?label=Total%20Downloads\&style=for-the-badge\&color=blue)](https://github.com/hitchin999/protector_net/releases)
[![Active Protector.Net Installs][prot-badge]][prot-analytics]

[prot-badge]: https://img.shields.io/badge/dynamic/json?label=Active%20Installs&url=https%3A%2F%2Fanalytics.home-assistant.io%2Fcustom_integrations.json&query=%24.protector_net.total&style=for-the-badge&color=blue
[prot-analytics]: https://analytics.home-assistant.io/integration/protector_net

# Protector.Net Access Control for Home Assistant

This custom integration controls **Hartmann Controls Protector.Net** door access systems via HTTP + a live **SignalR** websocket for instant updates.

---

## What’s new in 0.1.7

* **Fix:** Door sensors could fail to appear when the selected partition was named **“Default Partition.”** Discovery is now partition-scoped and resilient, so those sensors load correctly. No reconfiguration needed.
* **Reliability:** If an overview filter returns no doors at startup, discovery now falls back to the partition’s door list and retries after the hub reports mapped doors.

---

## What’s new in 0.1.6

* **Partition-scoped devices**

  * **Hub device** per configured partition: `Hub Status – <Partition>`
  * **Action Plans device** per partition: `Action Plans – <Partition>`
  * **All Doors device** per partition: `All Doors – <Partition>` with an **All Doors Lockdown** switch
  * Door entities remain grouped by **door device** *(unchanged)*

* **5 real-time sensors**

  1. **Hub Status – <Partition>** — `running / connecting / idle / stopped / error`
     *Attributes:* `phase`, `connected`, `mapped_doors`, `partition_id`
  2. **<Door> Lock State** — `Locked` / `Unlocked` (live strike/opener)
  3. **<Door> Overridden** — `On` / `Off`
  4. **<Door> Reader Mode** — friendly mapping of controller `timeZone`
  5. **<Door> Last Door Log by** — state is **who** last acted (e.g., “Home Assistant”, person/cardholder)
     *Attributes:* `Reader Message`, `Reader Message Time`, `Door Message`, `Door ID`, `Partition ID`

* **Legacy door buttons — simplified & selectable**

  * **Pulse Unlock** is **always included**.
  * Other legacy buttons are **optional** (pick them in **Options**):

    * Resume Schedule
    * Unlock Until Resume
    * Unlock Until Next Schedule
    * CardOrPin Until Resume
    * Timed Override Unlock (uses default minutes)
  * Nothing is pre-selected by default.

* **New per-door Override UI (synced instantly)**

  * **Override** *(switch)* — ON applies the selected override; OFF resumes schedule.
  * **Override Type** *(select)* — `For Specified Time` / `Until Resumed` / `Until Next Schedule`
  * **Override Mode** *(select)* — includes **`None`**
    OFF ⇒ shows **`None`**; ON ⇒ mirrors the live reader mode.
  * **Override Minutes** *(number)* — used when type is “For Specified Time”.
  * Internal dispatcher updates ensure **immediate UI refresh** (no need to leave/return the device page).

> **Heads-up:** Device names changed (partition-scoped). If you filtered by **device** in dashboards/automations, re-select the new device names. **Entity IDs / unique_ids remain stable.**

---

## Features

* ✅ Cookie login (`ss-id`) with automatic re-auth
* ✅ Partition selection (imports only your chosen partition)
* ✅ **Zero-polling** live updates via SignalR
* ✅ Door controls: per-door override UI + Pulse Unlock (+ optional legacy buttons)
* ✅ **All Doors Lockdown** switch (partition-wide)
* ✅ **HA Door Log** entries when you use HA buttons (e.g., “Home Assistant unlocked …”)
* ✅ All controls & options in the UI (HACS-friendly)

---

## Installation

**HACS (recommended):**

1. In **HACS → Integrations**, search for **“Protector.Net Access Control”** and install.
2. **Restart** Home Assistant.

**Manual:**

1. Copy `custom_components/protector_net/` into your Home Assistant `config/custom_components/`.
2. **Restart** Home Assistant.

Then go to **Settings → Devices & Services → Add Integration** → “Protector.Net Access Control”.

---

## Setup & Options

* **Base URL** – `https://host:port`
* **Credentials** – a Protector.Net user with sufficient privileges (**must be a System Administrator** in Hartmann)
* **Default override minutes** – used for Timed Override
* **Partition** – select exactly one
* **Action Plans** – pick trigger plans to clone as **System** plans (so they can be executed from HA)

Revisit any time: **Settings → Devices & Services → Protector.Net → Options**.

### Door Entities (legacy buttons)

* **Pulse Unlock** is always included (not shown in the picker).
* Choose any **additional** legacy buttons you want; **none** are pre-selected by default.

---

## Devices & Entities

### 1) Hub device (per partition)

* **Device:** `Hub Status – <Partition>`
* **Entity:** **Hub Status – <Partition>** *(sensor)*
  **State:** `running / connecting / idle / stopped / error`
  **Attributes:** `phase`, `connected`, `mapped_doors`, `partition_id`

### 2) Door devices (one per door)

**Sensors**

* **Lock State** — `Locked` / `Unlocked`
* **Overridden** — `On` / `Off`
* **Reader Mode** — mapped from controller index:
  `0/8 Lockdown, 1 Card, 2 Pin, 3 Card or Pin, 4 Card and Pin, 5 Unlock, 6 First Credential In, 7 Dual Credential`
* **Last Door Log by** — highlights the last actor; attributes include the last **reader/action** message/time and the last **door** message.

**Controls**

* **Override** *(switch)* — ON applies selected **Type** + **Mode** (and minutes if “For Specified Time”); OFF resumes schedule and forces **Override Mode = None**.
* **Override Type** *(select)* — `For Specified Time` / `Until Resumed` / `Until Next Schedule`
* **Override Mode** *(select)* — `None`, `Card`, `Pin`, `Unlock`, `Card and Pin`, `Card or Pin`, `First Credential In`, `Dual Credential`, `Lockdown`

  * OFF ⇒ shows **`None`**; ON ⇒ mirrors the panel’s current reader mode.
* **Override Minutes** *(number)* — used when type is “For Specified Time”

**Door Buttons**

* **Always:** Pulse Unlock
* **Optional (if selected in Options):** Resume Schedule, Unlock Until Resume, Unlock Until Next Schedule, CardOrPin Until Resume, Timed Override Unlock

### 3) Action Plans device (per partition)

* **Device:** `Action Plans – <Partition>`
* **Entities:** `Action Plan: <Plan Name>` *(button)* — executes cloned System-type plans.

### 4) All Doors device (per partition)

* **Device:** `All Doors – <Partition>`
* **Entity:** **All Doors Lockdown** *(switch)*
  **ON:** apply **Lockdown** override on **all doors** in the partition.
  **OFF:** **Resume Schedule** across all doors.

---

## How “Last Door Log by” works

* **State** becomes the **person/app** when:

  * Access granted/denied events, or
  * Action plan messages like “Home Assistant unlocked …”
* **Attributes** are stable and minimal:

  * `Reader Message`, `Reader Message Time` (granted/denied or action text + timestamp)
  * `Door Message` (e.g., “Door is now Locked/Unlocked”)
  * `Door ID`, `Partition ID`

Lock/Unlock **status** messages don’t flip the “by” state (that’s what **Lock State** is for).

---

## Troubleshooting

* **Sensors didn’t appear previously for “Default Partition”**
  Update to **0.1.7**; discovery now correctly loads those doors.

---

## Changelog

### 0.1.7

* Fix: Door sensors now load correctly when the selected partition is named **“Default Partition.”**
* Reliability: More robust, partition-scoped discovery with safe fallback and retry.

### 0.1.6

* New: **Partition-scoped devices** (Hub + Action Plans + **All Doors**)
* New: **All Doors Lockdown** switch (partition-wide)
* New: **5 sensors** total (Hub Status, Lock State, Overridden, Reader Mode, **Last Door Log by**)
* New: **Per-door Override UI** (Switch + Selects + Number), with **instant sync**
* Change: **Legacy buttons** refined — **Pulse Unlock always**; others **optional** via Options

### 0.1.5

* Door-action logs (“Home Assistant unlocked …”)

### 0.1.4

* Plan cloning fixes and reuse; HA Door Log plan

### 0.1.3

* Action Plans import/execute; options refresh

### 0.1.2

* Configurable door entities; options flow; base fixes

### 0.1.1

* Partition selection; session refresh; dynamic titles

### 0.1.0

* Initial release (doors & basic controls)

---

**Author:** Yoel Goldstein / Vaayer LLC
