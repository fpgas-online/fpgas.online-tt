"""Board-side register expressions that differ between the RP2040 and the RP2350 (MicroPython `machine.mem32`).

SIO's GPIO_OE (which GPIOs the chip drives) is at 0xd0000020 on the RP2040 (pico-sdk
rp2040/hardware_regs/include/hardware/regs/sio.h) but at 0xd0000030 on the RP2350
(rp2350/hardware_regs/include/hardware/regs/sio.h:380). On the RP2350, 0xd0000020 is the write-only
GPIO_OUT_CLR (sio.h:249, :254), so reading it says nothing about the outputs.

SYSINFO CHIP_ID is at 0x40000000 on both chips; its PART field (bits 27:12) is 0x0002 on the RP2040 and
0x0004 on the RP2350 (rp2040/.../regs/sysinfo.h:30 and rp2350/.../regs/sysinfo.h:30).

The expression needs only `machine` to be imported on the board.
"""

GPIO_OE_EXPR = "(0xd0000030 if (machine.mem32[0x40000000] >> 12) & 0xffff == 4 else 0xd0000020)"
