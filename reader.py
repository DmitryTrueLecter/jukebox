"""RC522 reader for one ISO14443A tag.

Uses the mfrc522 library's own MFRC522_Request + MFRC522_Anticoll —
the same calls that the original SimpleMFRC522.read_no_block() uses.
No SELECT, no HALT: tag returns to IDLE naturally after ~5 ms and the
next WUPA reliably finds it again.

UID is 4 bytes from anticollision (8 hex chars). For cascade tags
(NTAG215 etc.) byte 0 is 0x88; bytes 1-3 carry the unique part.
"""
import time


def crc_a(data):
    crc = 0x6363
    for value in data:
        value ^= crc & 0xFF
        value ^= (value << 4) & 0xFF
        crc = ((crc >> 8) ^ (value << 8) ^ (value << 3) ^ (value >> 4)) & 0xFFFF
    return [crc & 0xFF, crc >> 8]


class Reader:
    def __init__(self, chip=None):
        if chip is None:
            import RPi.GPIO as GPIO
            from mfrc522 import MFRC522
            chip = MFRC522(bus=0, device=0, pin_mode=GPIO.BOARD, pin_rst=22)
        self.chip = chip
        self.last_health = 0.0
        try:
            self.health()
            chip.Write_MFRC522(0x28, 0x3F)  # max CW TX power
            chip.Write_MFRC522(0x29, 0x3F)  # max modulation TX power
        except Exception:
            self.chip.Close_MFRC522()
            raise

    def health(self):
        version = self.chip.Read_MFRC522(0x37)
        if version in (0, 255):
            raise RuntimeError(f"RC522 не отвечает: VersionReg=0x{version:02X}")
        self.last_health = time.monotonic()
        return version

    def read(self):
        if time.monotonic() - self.last_health > 2:
            self.health()
        c = self.chip
        for _ in range(3):
            status, _ = c.MFRC522_Request(c.PICC_REQALL)
            if status == c.MI_OK:
                status, uid = c.MFRC522_Anticoll()
                if status == c.MI_OK:
                    uid_hex = bytes(uid[:4]).hex().upper()
                    halt = [0x50, 0x00]
                    halt += crc_a(halt)
                    c.MFRC522_ToCard(c.PCD_TRANSCEIVE, halt)
                    return uid_hex
            time.sleep(0.006)
        return None

    def close(self):
        self.chip.Close_MFRC522()
