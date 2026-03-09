import sys
import pygame
import threading
import queue
import cv2
import subprocess
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

class PrinterApp:
    def __init__(self):
        self.printer = PrinterController(port=PORT, baud_rate=BAUD_RATE)
        
        # We use a thread-safe Queue (maxsize=1) to prevent command overshoot
        self.cmd_queue = queue.Queue(maxsize=1)
        
        # State tracking
        self.current_pos = {"X": 0.0, "Y": 0.0, "Z": 0.0}
        self.running = False
        self.worker_thread = None
        self.cap = None

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
        responses = self.printer.send_gcode("M114", wait_for_ok=True)
        for line in responses:
            if "X:" in line and "Y:" in line and "Z:" in line:
                try:
                    for p in line.split():
                        if p.startswith("X:"): self.current_pos["X"] = float(p.split(":")[1])
                        if p.startswith("Y:"): self.current_pos["Y"] = float(p.split(":")[1])
                        if p.startswith("Z:"): self.current_pos["Z"] = float(p.split(":")[1])
                except Exception:
                    pass

    def setup_camera(self):
        """Initialize OpenCV camera capture."""
        print("Connecting to virtual camera...")
        
        obs_index = -1
        try:
            # Query the registry for DirectShow video capture devices (which OpenCV uses)
            ps_cmd = '(chcp 65001 >$null) ; Get-ItemProperty -Path "HKLM:\\SOFTWARE\\Classes\\CLSID\\{860BB310-5D01-11D0-BD3B-00A0C911CE86}\\Instance\\*" | Select-Object -ExpandProperty FriendlyName'
            result = subprocess.run(["powershell", "-NoProfile", "-Command", ps_cmd], capture_output=True, text=True, timeout=3)
            
            if result.returncode == 0:
                devices = [line.strip() for line in result.stdout.split('\n') if line.strip()]
                print(f"Detected cameras via PowerShell: {devices}")
                
                for i, name in enumerate(devices):
                    if "OBS Virtual Camera" in name:
                        obs_index = i
                        break
        except Exception as e:
            print(f"Failed to query cameras via powershell: {e}")

        if obs_index != -1:
            print(f"Found OBS Virtual Camera at index {obs_index}")
            self.cap = cv2.VideoCapture(obs_index, cv2.CAP_DSHOW)
        else:
            print("OBS Virtual Camera not found in system device list! Falling back to index 1.")
            self.cap = cv2.VideoCapture(1, cv2.CAP_DSHOW)
            
        if not self.cap.isOpened():
            print("Warning: Could not open camera device. Make sure OBS Virtual Camera is running.")

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

    def handle_keyboard_input(self):
        """Poll keys and queue movements."""
        if not self.cmd_queue.empty():
            return # Wait for current movement to finish

        keys = pygame.key.get_pressed()
        dx, dy, dz = 0, 0, 0
        f_rate = XY_FEEDRATE
        
        if keys[pygame.K_w]: dx = -XY_MOVE_MM
        if keys[pygame.K_s]: dx = XY_MOVE_MM
        if keys[pygame.K_a]: dy = -XY_MOVE_MM
        if keys[pygame.K_d]: dy = XY_MOVE_MM
        if keys[pygame.K_e]: dz = Z_MOVE_MM
        if keys[pygame.K_q]: dz = -Z_MOVE_MM
        
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

    def run_ui(self):
        """Main Pygame execution loop."""
        print("Printer initialized! Move focus to the Pygame window.")
        
        pygame.init()
        screen = pygame.display.set_mode((WINDOW_WIDTH, WINDOW_HEIGHT))
        pygame.display.set_caption("3D Printer WASD Low Latency")
        clock = pygame.time.Clock()
        
        font = pygame.font.SysFont(None, 48)
        small_font = pygame.font.SysFont(None, 24)
        
        while self.running:
            # Try to grab and display a camera frame, otherwise fallback to solid background
            frame_rendered = False
            if self.cap and self.cap.isOpened():
                ret, frame = self.cap.read()
                if ret:
                    # OpenCV uses BGR, Pygame needs RGB
                    frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                    # Convert to pygame surface directly from the byte buffer
                    frame_surface = pygame.image.frombuffer(frame.tobytes(), (frame.shape[1], frame.shape[0]), "RGB")
                    # Draw the frame exactly at its native resolution
                    screen.blit(frame_surface, (0, 0))
                    frame_rendered = True
            
            if not frame_rendered:
                screen.fill((30, 30, 50))
            
            # Render HUD
            pos_text = font.render(f"X: {self.current_pos['X']:.1f}   Y: {self.current_pos['Y']:.1f}   Z: {self.current_pos['Z']:.1f}", True, (100, 255, 100))
            screen.blit(pos_text, (20, 20))
            
            ctrl_text = small_font.render("Hold W/S (X), A/D (Y), E/Q (Z)   ESC to Exit", True, (150, 150, 150))
            screen.blit(ctrl_text, (20, 80))
            
            home_text = small_font.render("Press SPACE to set current location as Origin (0,0,0)", True, (255, 150, 150))
            screen.blit(home_text, (20, 110))

            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    self.running = False
                elif event.type == pygame.KEYDOWN:
                    if event.key == pygame.K_ESCAPE:
                        self.running = False
                    elif event.key == pygame.K_SPACE:
                        self.clear_queue()
                        print("\nResetting Origin to current location...")
                        self.cmd_queue.put_nowait("G92 X0 Y0 Z0")
                        self.current_pos = {"X": 0.0, "Y": 0.0, "Z": 0.0}

            self.handle_keyboard_input()
            
            pygame.display.flip()
            clock.tick(60)

    def shutdown(self):
        """Clean up threads and printer connection."""
        print("\nCleaning up...")
        self.running = False
        pygame.quit()
        
        if self.cap:
            self.cap.release()
        
        if self.worker_thread and self.worker_thread.is_alive():
            self.cmd_queue.put("QUIT")
            self.worker_thread.join(timeout=1.0)
            
        self.printer.set_absolute_positioning()
        self.printer.disconnect()


def main():
    app = PrinterApp()
    try:
        app.setup_printer()
        app.run_ui()
    except PrinterError as e:
        print(f"\nPrinter Error: {e}")
    except KeyboardInterrupt:
        print("\nForce quit detected.")
    finally:
        app.shutdown()

if __name__ == "__main__":
    main()

