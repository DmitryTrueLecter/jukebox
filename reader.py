"""RC522 reader for one ISO14443A tag.

Uses the mfrc522 library's own MFRC522_Request + MFRC522_Anticoll —
the same calls that the original SimpleMFRC522.read_no_block() uses —
then sends HALT so the tag answers the next poll's REQALL (WUPA) on the
first try instead of after a retry.

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
        self.present = False  # a tag answered on the previous read()
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
        # Retries only guard a present tag against a one-off miss; an empty
        # reader gets one attempt per poll (each miss busy-polls SPI for ms).
        attempts = 3 if self.present else 1
        for attempt in range(attempts):
            status, _ = c.MFRC522_Request(c.PICC_REQALL)
            if status == c.MI_OK:
                status, uid = c.MFRC522_Anticoll()
                if status == c.MI_OK:
                    self._halt()
                    self.present = True
                    return bytes(uid[:4]).hex().upper()
            if attempt + 1 < attempts:
                time.sleep(0.006)
        self.present = False
        return None

    def _halt(self):
        # A tag never answers HALT, so send it with Transmit (0x04) instead of the
        # library's Transceive, which busy-polls SPI ~10 ms waiting for a reply.
        c = self.chip
        frame = [0x50, 0x00]
        frame += crc_a(frame)
        c.Write_MFRC522(c.CommandReg, c.PCD_IDLE)
        c.SetBitMask(c.FIFOLevelReg, 0x80)  # flush FIFO
        for value in frame:
            c.Write_MFRC522(c.FIFODataReg, value)
        c.Write_MFRC522(c.BitFramingReg, 0x00)
        c.Write_MFRC522(c.CommandReg, 0x04)
        time.sleep(0.001)  # 4 bytes at 106 kbit/s take ~0.4 ms
        c.Write_MFRC522(c.CommandReg, c.PCD_IDLE)

    def close(self):
        self.chip.Close_MFRC522()
