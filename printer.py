import serial
import time

class PrinterError(Exception):
    pass

class PrinterController:
    """A wrapper class to control a 3D printer over serial connection."""
    
    def __init__(self, port='COM3', baud_rate=115200, timeout=2):
        self.port = port
        self.baud_rate = baud_rate
        self.timeout = timeout
        self.serial_conn = None

    def connect(self):
        """Establish connection with the printer."""
        try:
            print(f"Connecting to printer on {self.port} at {self.baud_rate} baud...")
            self.serial_conn = serial.Serial(self.port, self.baud_rate, timeout=self.timeout)
            
            # Most printers reset upon USB serial connection
            print("Waiting for printer to boot/initialize...")
            time.sleep(3)
            
            # Flush any bootloader junk
            self.serial_conn.reset_input_buffer()
            print("Connected successfully.")
        except serial.SerialException as e:
            raise PrinterError(f"Failed to connect to {self.port}: {e}")

    def disconnect(self):
        """Close the serial connection."""
        if self.serial_conn and self.serial_conn.is_open:
            self.serial_conn.close()
            print("Disconnected.")

    def send_gcode(self, command, wait_for_ok=True):
        """Send a single G-code command and optionally wait for 'ok'."""
        if not self.serial_conn or not self.serial_conn.is_open:
            raise PrinterError("Not connected to printer.")
            
        print(f"Sending: {command}")
        self.serial_conn.write(f"{command}\n".encode('utf-8'))
        
        responses = []
        if wait_for_ok:
            while True:
                response = self.serial_conn.readline().decode('utf-8', errors='ignore').strip()
                if response:
                    print(f"Printer: {response}")
                    responses.append(response)
                
                # 'ok' signals the printer is ready for the next command
                # It can sometimes be 'ok ...' on some firmware
                if response.startswith("ok"):
                    break
                    
        return responses

    # --- Common Movement Commands ---
    
    def home_all(self):
        """Auto-home all axes (X, Y, Z)."""
        return self.send_gcode("G28")

    def home(self, x=False, y=False, z=False):
        """Home specific axes."""
        axes = []
        if x: axes.append("X")
        if y: axes.append("Y")
        if z: axes.append("Z")
        cmd = f"G28 {' '.join(axes)}" if axes else "G28"
        return self.send_gcode(cmd)

    def set_absolute_positioning(self):
        """Set movement to absolute coordinates (G90)."""
        return self.send_gcode("G90")

    def set_relative_positioning(self):
        """Set movement to relative coordinates (G91)."""
        return self.send_gcode("G91")

    def get_position(self):
        """Request and parse accurate absolute coordinates (M114)."""
        responses = self.send_gcode("M114", wait_for_ok=True)
        pos = {"X": 0.0, "Y": 0.0, "Z": 0.0}
        for line in responses:
            if "X:" in line and "Y:" in line and "Z:" in line:
                try:
                    for p in line.split():
                        if p.startswith("X:"): pos["X"] = float(p.split(":")[1])
                        if p.startswith("Y:"): pos["Y"] = float(p.split(":")[1])
                        if p.startswith("Z:"): pos["Z"] = float(p.split(":")[1])
                except Exception:
                    pass
        return pos

    def move(self, x=None, y=None, z=None, e=None, f=None):
        """
        Move to/by coordinates. 
        f = Feedrate (speed) in mm/min.
        """
        cmd = ["G1"]
        if f is not None: cmd.append(f"F{f}")
        if x is not None: cmd.append(f"X{x}")
        if y is not None: cmd.append(f"Y{y}")
        if z is not None: cmd.append(f"Z{z}")
        if e is not None: cmd.append(f"E{e}")
        
        if len(cmd) > 1:
            return self.send_gcode(" ".join(cmd))

    # --- Temperature Commands ---

    def get_temperatures(self):
        """Request current temperatures (M105)."""
        return self.send_gcode("M105")

    def set_nozzle_temp(self, temp, wait=False):
        """Set the hotend/nozzle temperature. wait=True to block until temp reached."""
        if wait:
            return self.send_gcode(f"M109 S{temp}")
        else:
            return self.send_gcode(f"M104 S{temp}")

    def set_bed_temp(self, temp, wait=False):
        """Set the heated bed temperature. wait=True to block until temp reached."""
        if wait:
            return self.send_gcode(f"M190 S{temp}")
        else:
            return self.send_gcode(f"M140 S{temp}")

    # --- Utility Commands ---
    
    def motors_off(self):
        """Disable all stepper motors (M84)."""
        return self.send_gcode("M84")

    def fan_on(self, speed=255):
        """Turn on the part cooling fan. speed 0-255 (M106)."""
        return self.send_gcode(f"M106 S{speed}")
        
    def fan_off(self):
        """Turn off the part cooling fan (M107)."""
        return self.send_gcode("M107")
