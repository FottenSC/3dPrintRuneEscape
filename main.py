import sys
import threading
import queue
import cv2
import subprocess
from PyQt6.QtWidgets import (QApplication, QMainWindow, QLabel, QWidget, QMenuBar, 
                             QDialog, QVBoxLayout, QFormLayout, QComboBox, QPushButton, 
                             QKeySequenceEdit, QGroupBox, QGridLayout, QHBoxLayout)
from PyQt6.QtCore import QTimer, Qt, QSettings
from PyQt6.QtGui import QImage, QPixmap, QFont, QKeySequence, QAction
from printer import PrinterController, PrinterError

# Configuration
PORT = 'COM3'
BAUD_RATE = 115200
WINDOW_WIDTH = 1280
WINDOW_HEIGHT = 720

# Movement settings
XY_MOVE_MM = 1.0     
XY_FEEDRATE = 6000   
Z_MOVE_MM = 0.1     
Z_FEEDRATE = 600

def get_available_cameras():
    """Query Windows registry for DirectShow cameras."""
    cameras = {}
    try:
        ps_cmd = '(chcp 65001 >$null) ; Get-ItemProperty -Path "HKLM:\\SOFTWARE\\Classes\\CLSID\\{860BB310-5D01-11D0-BD3B-00A0C911CE86}\\Instance\\*" | Select-Object -ExpandProperty FriendlyName'
        result = subprocess.run(["powershell", "-NoProfile", "-Command", ps_cmd], capture_output=True, text=True, timeout=3)
        if result.returncode == 0:
            lines = [line.strip() for line in result.stdout.split('\n') if line.strip()]
            for i, name in enumerate(lines):
                cameras[name] = i
    except Exception as e:
        print(f"Failed to query cameras: {e}")
    return cameras

class SettingsDialog(QDialog):
    def __init__(self, parent=None, current_cam_idx=-1, keybinds=None):
        super().__init__(parent)
        self.setWindowTitle("Preferences")
        self.setMinimumWidth(400)
        
        main_layout = QVBoxLayout(self)
        
        # --- Camera Section ---
        cam_group = QGroupBox("Video Input")
        cam_layout = QVBoxLayout()
        self.cam_combo = QComboBox()
        self.cameras = get_available_cameras()
        
        self.cam_combo.addItem("Unassigned (Disabled)", -1)
        for name, idx in self.cameras.items():
            self.cam_combo.addItem(f"{name}", idx)
            
        current_idx = self.cam_combo.findData(current_cam_idx)
        self.cam_combo.setCurrentIndex(current_idx if current_idx >= 0 else 0)
        
        cam_layout.addWidget(self.cam_combo)
        cam_group.setLayout(cam_layout)
        main_layout.addWidget(cam_group)
        
        # --- Keybinds Section ---
        keys_group = QGroupBox("Controls")
        keys_layout = QGridLayout()
        
        # Re-organize keys into a nicely aligned grid
        self.key_edits = {}
        row = 0
        col = 0
        
        # Group definitions for visual layout
        key_sets = [
            ("X Axis (Left/Right)", ['X-', 'X+']),
            ("Y Axis (Forward/Back)", ['Y-', 'Y+']),
            ("Z Axis (Up/Down)", ['Z-', 'Z+']),
            ("Utility", ['Set Origin'])
        ]
        
        for category, binds in key_sets:
            keys_layout.addWidget(QLabel(f"<b>{category}</b>"), row, 0, 1, 2)
            row += 1
            for action in binds:
                keys_layout.addWidget(QLabel(action), row, 0)
                edit = QKeySequenceEdit(QKeySequence(keybinds[action]))
                edit.setMaximumWidth(150)
                self.key_edits[action] = edit
                keys_layout.addWidget(edit, row, 1)
                row += 1
            # Add some spacing between groups
            keys_layout.setRowMinimumHeight(row, 10)
            row += 1

        keys_group.setLayout(keys_layout)
        main_layout.addWidget(keys_group)
        
        # --- Bottom Buttons ---
        btn_layout = QHBoxLayout()
        
        # Auto-detect OBS index for Restore button
        self.obs_idx = -1
        for name, idx in self.cameras.items():
            if "OBS Virtual Camera" in name:
                self.obs_idx = idx
                break
                
        self.restore_btn = QPushButton("Restore Defaults")
        self.restore_btn.clicked.connect(self.restore_defaults)
        
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.clicked.connect(self.reject)
        
        self.save_btn = QPushButton("Save && Apply")
        self.save_btn.setDefault(True) # Highlights it as the primary action
        self.save_btn.clicked.connect(self.accept)
        
        # Push save/cancel to the right, restore to the left
        btn_layout.addWidget(self.restore_btn)
        btn_layout.addStretch()
        btn_layout.addWidget(self.cancel_btn)
        btn_layout.addWidget(self.save_btn)
        
        main_layout.addLayout(btn_layout)

    def restore_defaults(self):
        """Reset inputs to their original default states."""
        # Reset camera
        idx = self.cam_combo.findData(self.obs_idx)
        if idx >= 0:
            self.cam_combo.setCurrentIndex(idx)
        else:
            self.cam_combo.setCurrentIndex(0) # Unassigned

        # Reset keys
        default_keys = {
            'X+': Qt.Key.Key_S.value,
            'X-': Qt.Key.Key_W.value,
            'Y+': Qt.Key.Key_D.value,
            'Y-': Qt.Key.Key_A.value,
            'Z+': Qt.Key.Key_E.value,
            'Z-': Qt.Key.Key_Q.value,
            'Set Origin': Qt.Key.Key_Space.value
        }
        for action, edit in self.key_edits.items():
            if action in default_keys:
                edit.setKeySequence(QKeySequence(default_keys[action]))

    def get_settings(self):
        new_binds = {}
        for action, edit in self.key_edits.items():
            seq = edit.keySequence()
            if not seq.isEmpty():
                # Extract the bare key int format that PyQt events output
                val = seq[0]
                new_binds[action] = val.key().value if hasattr(val, 'key') else int(val)
            else:
                new_binds[action] = 0
                
        return self.cam_combo.currentData(), new_binds

class PrinterApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.printer = PrinterController(port=PORT, baud_rate=BAUD_RATE)
        
        # We use a thread-safe Queue (maxsize=1) to prevent command overshoot
        self.cmd_queue = queue.Queue(maxsize=1)
        
        # State tracking
        self.current_pos = {"X": 0.0, "Y": 0.0, "Z": 0.0}
        self.running = False
        self.worker_thread = None
        self.cap = None
        self.keys_pressed = set()
        
        self.settings = QSettings("3DPrintRuneEscape", "WASDController")
        
        # Load from QSettings if available
        self.current_cam_idx = self.settings.value("camera_index", -1, type=int)
        self.settings_loaded = self.settings.contains("camera_index")

        default_keys = {
            'X+': Qt.Key.Key_S.value,
            'X-': Qt.Key.Key_W.value,
            'Y+': Qt.Key.Key_D.value,
            'Y-': Qt.Key.Key_A.value,
            'Z+': Qt.Key.Key_E.value,
            'Z-': Qt.Key.Key_Q.value,
            'Set Origin': Qt.Key.Key_Space.value
        }
        
        self.keybinds = {}
        self.settings.beginGroup("keybinds")
        for action, default_val in default_keys.items():
            self.keybinds[action] = self.settings.value(action, default_val, type=int)
        self.settings.endGroup()
        
        # UI Setup
        self.setWindowTitle("3D Printer WASD Low Latency (PyQt)")
        self.setFixedSize(WINDOW_WIDTH, WINDOW_HEIGHT)

        # Menu Bar
        menu_bar = self.menuBar()
        settings_menu = menu_bar.addMenu("Settings")
        prefs_action = QAction("Preferences...", self)
        prefs_action.triggered.connect(self.open_settings)
        settings_menu.addAction(prefs_action)
        
        tools_menu = menu_bar.addMenu("Tools")
        screen_action = QAction("Copy Screenshot to Clipboard", self)
        screen_action.triggered.connect(self.copy_screenshot)
        tools_menu.addAction(screen_action)
        
        # Central widget container for UI layers
        central = QWidget()
        self.setCentralWidget(central)
        
        # We need a layout so the video automatically fills the available space
        # without being cut off by the height of the MenuBar!
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(0, 0, 0, 0)
        
        self.video_label = QLabel()
        self.video_label.setStyleSheet("background-color: rgb(30, 30, 50);")
        self.video_label.setScaledContents(True)  # Ensures the video scales to fill the 16:9 window
        main_layout.addWidget(self.video_label)
        
        # HUD remains absolute positioned on top of the layout
        self.hud_label = QLabel(central)
        self.hud_label.setGeometry(20, 20, 800, 50)
        font = QFont()
        font.setPointSize(36)
        font.setBold(True)
        self.hud_label.setFont(font)
        self.hud_label.setStyleSheet("color: rgb(100, 255, 100); background-color: rgba(0, 0, 0, 100); padding: 5px;")
        
        # Main updates clock (60 fps)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.update_loop)

    def open_settings(self):
        """Open settings dialog to change webcam and keybinds."""
        self.keys_pressed.clear() # Prevent stuck keys while dialog is open
        dialog = SettingsDialog(self, self.current_cam_idx, self.keybinds)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            new_cam_idx, new_binds = dialog.get_settings()
            
            # Update keybinds
            self.keybinds = new_binds
            
            # Update camera if changed
            if new_cam_idx != self.current_cam_idx:
                self.current_cam_idx = new_cam_idx
                self.apply_camera()
                
            # Save to QSettings
            self.settings.setValue("camera_index", self.current_cam_idx)
            self.settings.beginGroup("keybinds")
            for action, val in self.keybinds.items():
                self.settings.setValue(action, int(val))
            self.settings.endGroup()
            self.settings.sync()
    def copy_screenshot(self):
        """Grabs the current frame from OpenCV and copies it to the clipboard."""
        if not self.cap or not self.cap.isOpened():
            print("No active camera to screenshot.")
            return
            
        ret, frame = self.cap.read()
        if ret:
            frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            h, w, ch = frame_rgb.shape
            bytes_per_line = ch * w
            qimg = QImage(frame_rgb.data, w, h, bytes_per_line, QImage.Format.Format_RGB888)
            clipboard = QApplication.clipboard()
            clipboard.setImage(qimg.copy())
            print("Screenshot copied to clipboard!")
        else:
            print("Failed to capture frame for screenshot.")
    def worker_loop(self):
        """Background thread to process commands and sync position when idle."""
        needs_sync = True
        while self.running:
            try:
                cmd = self.cmd_queue.get(timeout=0.05)
                if cmd == "QUIT":
                    self.cmd_queue.task_done()
                    break
                
                self.printer.send_gcode(cmd, wait_for_ok=True)
                needs_sync = True
                self.cmd_queue.task_done()
            except queue.Empty:
                if needs_sync:
                    self._sync_position()
                    needs_sync = False

    def _sync_position(self):
        """Ask printer firmware for accurate absolute coordinates."""
        pos = self.printer.get_position()
        if pos:
            # Only update if we successfully parsed the position
            if "X" in pos: self.current_pos["X"] = pos["X"]
            if "Y" in pos: self.current_pos["Y"] = pos["Y"]
            if "Z" in pos: self.current_pos["Z"] = pos["Z"]

    def setup_camera(self):
        """Initialize OpenCV camera capture initially."""
        print("Connecting to virtual camera...")
        
        # Give priority to saved camera settings, otherwise fall back to auto finding OBS
        if not hasattr(self, 'settings_loaded') or not self.settings_loaded or self.current_cam_idx == -1:
            cameras = get_available_cameras()
            obs_index = -1
            for name, idx in cameras.items():
                if "OBS Virtual Camera" in name:
                    obs_index = idx
                    break
                    
            if obs_index != -1:
                print(f"Found OBS Virtual Camera at index {obs_index}")
                self.current_cam_idx = obs_index
            elif not self.settings_loaded:
                print("OBS Virtual Camera not found! Disabling camera until selected in settings.")
                self.current_cam_idx = -1
            
        if self.current_cam_idx != -1:
            self.apply_camera()

    def apply_camera(self):
        """Release current and open the selected camera index."""
        if self.cap:
            self.cap.release()
            
        if self.current_cam_idx == -1:
            print("Camera is set to unassigned/disabled.")
            self.video_label.clear()
            return

        print(f"Opening camera index {self.current_cam_idx}")
        self.cap = cv2.VideoCapture(self.current_cam_idx, cv2.CAP_DSHOW)
        if not self.cap.isOpened():
            print("Warning: Could not open camera device.")
        else:
            # Force OpenCV to request high resolution (16:9) from the virtual camera
            self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, WINDOW_WIDTH)
            self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, WINDOW_HEIGHT)

    def setup_printer(self):
        """Connect and initialize printer coordinates."""
        self.printer.connect()
        
        print("Marker mode enabled: Skipping physical Auto-Home (G28).")
        print("Setting current physical location as Origin (G92 X0 Y0 Z0)...")
        
        self.printer.send_gcode("G92 X0 Y0 Z0")
        self.printer.set_relative_positioning()
        
        # Start camera stream
        self.setup_camera()
        
        self.running = True
        self.worker_thread = threading.Thread(target=self.worker_loop, daemon=True)
        self.worker_thread.start()
        
        self.timer.start(16) # ~60 fps

    def keyPressEvent(self, event):
        """Track keys being held down."""
        if not event.isAutoRepeat():
            self.keys_pressed.add(event.key())
            
        if event.key() == self.keybinds.get('Set Origin'):
            self.clear_queue()
            print("\nResetting Origin to current location...")
            self.cmd_queue.put_nowait("G92 X0 Y0 Z0")
            self.current_pos = {"X": 0.0, "Y": 0.0, "Z": 0.0}

    def keyReleaseEvent(self, event):
        """Untrack released keys."""
        if not event.isAutoRepeat() and event.key() in self.keys_pressed:
            self.keys_pressed.remove(event.key())

    def update_loop(self):
        """Main UI updates (runs on QTimer)."""
        # Render Camera
        if self.cap and self.cap.isOpened():
            ret, frame = self.cap.read()
            if ret:
                frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                h, w, ch = frame.shape
                bytes_per_line = ch * w
                qimg = QImage(frame.data, w, h, bytes_per_line, QImage.Format.Format_RGB888)
                self.video_label.setPixmap(QPixmap.fromImage(qimg))
        
        # Render HUD
        self.hud_label.setText(f"X: {self.current_pos['X']:.1f}   Y: {self.current_pos['Y']:.1f}   Z: {self.current_pos['Z']:.1f}")
        self.hud_label.adjustSize()
        
        # Keyboard Polling
        self.handle_continuous_keyboard()

    def handle_continuous_keyboard(self):
        """Poll grabbed keys and queue movements."""
        if not self.cmd_queue.empty():
            return # Wait for current movement to finish

        dx, dy, dz = 0, 0, 0
        f_rate = XY_FEEDRATE
        
        if self.keybinds.get('X-') in self.keys_pressed: dx = -XY_MOVE_MM
        if self.keybinds.get('X+') in self.keys_pressed: dx = XY_MOVE_MM
        if self.keybinds.get('Y-') in self.keys_pressed: dy = -XY_MOVE_MM
        if self.keybinds.get('Y+') in self.keys_pressed: dy = XY_MOVE_MM
        if self.keybinds.get('Z+') in self.keys_pressed: dz = Z_MOVE_MM
        if self.keybinds.get('Z-') in self.keys_pressed: dz = -Z_MOVE_MM
        
        if dx == 0 and dy == 0 and dz != 0:
            f_rate = Z_FEEDRATE
        
        if dx != 0 or dy != 0 or dz != 0:
            self._queue_move(dx, dy, dz, f_rate)

    def _queue_move(self, dx, dy, dz, feedrate):
        """Constructs G-code and pushes it to the worker."""
        parts = ["G1", f"F{feedrate}"]
        if dx != 0: parts.append(f"X{dx}")
        if dy != 0: parts.append(f"Y{dy}")
        if dz != 0: parts.append(f"Z{dz}")
        
        try:
            self.cmd_queue.put_nowait(" ".join(parts))
            
            # Dead-reckoning for 0 latency UI feedback
            self.current_pos['X'] += dx
            self.current_pos['Y'] += dy
            self.current_pos['Z'] += dz
            sys.stdout.write(f"\rQueueing move... dx:{dx} dy:{dy} dz:{dz}   ")
            sys.stdout.flush()
        except queue.Full:
            pass

    def clear_queue(self):
        """Flush any pending commands."""
        while not self.cmd_queue.empty():
            try: self.cmd_queue.get_nowait()
            except: pass

    def closeEvent(self, event):
        """Clean up on window close."""
        self.shutdown()
        event.accept()

    def shutdown(self):
        """Clean up threads and printer connection."""
        print("\nCleaning up...")
        self.running = False
        self.timer.stop()
        
        if self.cap:
            self.cap.release()
        
        if self.worker_thread and self.worker_thread.is_alive():
            self.cmd_queue.put("QUIT")
            self.worker_thread.join(timeout=1.0)
            
        self.printer.set_absolute_positioning()
        self.printer.disconnect()


def main():
    app = QApplication(sys.argv)
    
    printer_window = PrinterApp()
    try:
        printer_window.setup_printer()
        printer_window.show()
        print("Printer initialized! Move focus to the PyQt window.")
        sys.exit(app.exec())
        
    except PrinterError as e:
        print(f"\nPrinter Error: {e}")
        printer_window.shutdown()
    except KeyboardInterrupt:
        print("\nForce quit detected.")
        printer_window.shutdown()

if __name__ == "__main__":
    main()
