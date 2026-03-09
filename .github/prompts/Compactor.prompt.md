---
name: Compactor
description: Project context, history, and architectural summary for the 3D Printer Wasd Controller
---

# Project Context: 3D Printer WASD / Trackpad Controller

## Overview
This project transforms a standard USB-connected 3D printer into a low-latency, manually controlled plotter/manipulator. It is designed to physically interact with external interfaces (like a MacBook trackpad) using a custom-attached pointer. 

## Tech Stack
*   **Python Manager:** `uv`
*   **Printer Communication:** `pyserial` (sending G-code over COM port, usually 115200 baud).
*   **User Interface:** `pygame` (handles keyboard polling, UI rendering, and HUD overlays).
*   **Video Feed:** `opencv-python` (cv2) and `numpy` (reads an OBS Virtual Camera feed and draws it unscaled in the Pygame background).
*   **Native System Calls:** `subprocess` running PowerShell to scrape the Windows Registry for DirectShow Webcams (bypassing heavy 3rd party dependencies like `pygrabber`).

## Key Architectural Decisions
1.  **Low-Latency Threading & Queueing:**
    *   Standard `G1` movements lock up the main execution thread while waiting for the printer to reply with `ok`.
    *   Instead, the UI runs at 60fps in the main thread and polls `pygame.key.get_pressed()`.
    *   Movements are dumped into a background worker thread via a `queue.Queue(maxsize=1)`. 
    *   *The `maxsize=1` is critical:* It prevents the UI from queuing hundreds of movements while a key is held, which would cause massive hardware overshoot when the key is released.
2.  **Micro-Movements:**
    *   XY axes (belt-driven, fast) move in `1.0mm` chunks at `F6000`.
    *   Z axis (lead-screw, slow) moves in `0.1mm` chunks at `F600` to prevent input lag.
3.  **Zero-Latency UI Tracking:**
    *   While moving, the Pygame HUD uses dead-reckoning to update X/Y/Z coordinates instantly.
    *   Once the queue is fully idle, the background thread fires `M114` to fetch the true absolute position from the firmware to correct any drift.

## Hardware & Real-World Accommodations
*   **Marker Mode Homing:** Because a physical marker/pointer extends below the nozzle/probe, standard `G28` auto-homing would crash the tool into the bed. Instead, we use `G92 X0 Y0 Z0` to set a "Fake Origin" wherever the printhead is manually placed upon startup. Pressing `SPACEBAR` resets this origin.
*   **Capacitive Trackpad Pointer:** To make the 3D printer actuate a Mac trackpad (which requires a wide, soft conductive footprint to mimic human flesh), the pointer tip needs to use materials like:
    1. A capacitive stylus rubber tip.
    2. Anti-static ESD foam glued to the tip.
    3. Wrapped in conductive copper tape attached to the grounded frame of the printer.

## Camera Integration
*   The Pygame UI acts as an overlay.
*   It dynamically finds the "OBS Virtual Camera" using a native PowerShell registry query: `Get-ItemProperty -Path "HKLM:\SOFTWARE\Classes\CLSID\{860BB310-5D01-11D0-BD3B-00A0C911CE86}\Instance\*"`
*   The camera frames are converted from BGR to RGB and drawn pixel-for-pixel at native resolution in the Pygame background.