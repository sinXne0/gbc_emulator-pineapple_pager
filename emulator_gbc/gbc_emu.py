#!/usr/bin/env python3
"""
Zelda: Link's Awakening DX — native GBC emulator for WiFi Pineapple Pager
Renders to the physical 480×222 LCD via libpagerctl.so / pagerctl.py

Button mapping:
  Pager UP/DOWN/LEFT/RIGHT → GBC D-pad
  Pager A (green)          → GBC A
  Pager B (red)            → GBC B
  A + RIGHT                → GBC START   (open menu/save)
  A + LEFT                 → GBC SELECT  (map)
  A + B held 2 s           → quit
"""

import os, sys, time, traceback

# Log file alongside the script so it's easy to find after a crash
_LOG = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'zelda.log'), 'w', buffering=1)
def _log(*a): msg = ' '.join(str(x) for x in a); print(msg); _LOG.write(msg+'\n'); _LOG.flush()
sys.stderr = _LOG   # capture tracebacks too
_log("[zelda] starting")

# ──────────────────────────────────────────────────────────────
# DISPLAY CONSTANTS
# ──────────────────────────────────────────────────────────────
GBC_W,  GBC_H   = 160, 144
PAGER_W, PAGER_H = 480, 222
# 1.5× scale: 240×216, centred at (120, 3)
DISP_W, DISP_H  = 240, 216
OFF_X = (PAGER_W - DISP_W) // 2   # 120
OFF_Y = (PAGER_H - DISP_H) // 2   # 3
CYCLES_PER_FRAME = 70224

# Pre-built nearest-neighbour scale tables
SCALE_X = [x * GBC_W // DISP_W for x in range(DISP_W)]
SCALE_Y = [y * GBC_H // DISP_H for y in range(DISP_H)]

# fb0 stride — may be wider than PAGER_W*2 due to hardware alignment
# Probed at runtime in main(); blit() uses this global
FB_STRIDE = PAGER_W * 2

# Pager button bits
P_UP = 0x01; P_DN = 0x02; P_L = 0x04; P_R = 0x08
P_A  = 0x10; P_B  = 0x20

# GBC joypad bits (active-low)
# Direction group (P15 low / bit5=0): RIGHT=0, LEFT=1, UP=2, DOWN=3
# Action  group  (P14 low / bit4=0): A=0,     B=1, SELECT=2, START=3


# ──────────────────────────────────────────────────────────────
# MBC3
# ──────────────────────────────────────────────────────────────
class MBC3:
    def __init__(self, rom):
        self.rom      = rom
        self.bank     = 1
        self.ram      = bytearray(0x8000)   # 4 × 8 KB
        self.ram_bank = 0
        self.ram_en   = False

    def read(self, a):
        if a < 0x4000: return self.rom[a]
        if a < 0x8000:
            off = self.bank * 0x4000 + a - 0x4000
            return self.rom[off] if off < len(self.rom) else 0xFF
        if 0xA000 <= a < 0xC000 and self.ram_en and self.ram_bank < 4:
            return self.ram[self.ram_bank * 0x2000 + a - 0xA000]
        return 0xFF

    def write(self, a, v):
        if   a < 0x2000: self.ram_en   = (v & 0x0F) == 0x0A
        elif a < 0x4000: self.bank     = max(1, v & 0x7F)
        elif a < 0x6000:
            if v < 4: self.ram_bank = v
        elif 0xA000 <= a < 0xC000 and self.ram_en and self.ram_bank < 4:
            self.ram[self.ram_bank * 0x2000 + a - 0xA000] = v


# ──────────────────────────────────────────────────────────────
# GAME BOY (SM83 CPU + GBC PPU + Timer + Joypad)
# ──────────────────────────────────────────────────────────────
class GameBoy:

    def __init__(self, rom_path):
        with open(rom_path, 'rb') as f:
            rom = bytearray(f.read())
        self.mbc = MBC3(rom)

        # RAM
        self.vram  = [bytearray(0x2000), bytearray(0x2000)]
        self.wram  = [bytearray(0x1000) for _ in range(8)]
        self.oam   = bytearray(0xA0)
        self.hram  = bytearray(0x7F)
        self.io    = bytearray(0x80)
        self.ie    = 0
        self.if_   = 0xE1

        self.vram_bank = 0
        self.wram_bank = 1

        # CPU — GBC post-boot values
        self.a = 0x11; self.f = 0xB0
        self.b = 0x00; self.c = 0x13
        self.d = 0x00; self.e = 0xD8
        self.h = 0x01; self.l = 0x4D
        self.sp = 0xFFFE; self.pc = 0x0100
        self.ime = False; self.ime_p = False; self.halted = False

        # PPU
        self.scyc   = 0           # scanline cycle counter
        self.wlc    = 0           # window line counter
        self._draw  = True        # set False to skip pixel rendering
        self.fbuf   = bytearray(GBC_W * GBC_H * 2)   # RGB565
        self.bgp    = bytearray(64)    # 8 bg  palettes × 4 colours × 2 bytes
        self.obp    = bytearray(64)    # 8 obj palettes
        self.bgpi   = 0;  self.bgpi_a = False
        self.obpi   = 0;  self.obpi_a = False

        # Joypad (active-low)
        self.joy_dir = 0xFF   # P15 group: RIGHT/LEFT/UP/DOWN at bits 0-3
        self.joy_act = 0xFF   # P14 group: A/B/SEL/START    at bits 0-3

        # Timer
        self.div_c  = 0
        self.tima_c = 0

        # IO defaults
        io = self.io
        io[0x00] = 0xFF; io[0x04] = 0x00; io[0x07] = 0xF8
        io[0x40] = 0x91; io[0x41] = 0x85; io[0x44] = 0x00; io[0x45] = 0x00
        io[0x4D] = 0x7E; io[0x4F] = 0xFE; io[0x70] = 0xF9

    # ── Memory ────────────────────────────────────────────────
    def rb(self, a):
        a &= 0xFFFF
        if a < 0x8000: return self.mbc.read(a)
        if a < 0xA000: return self.vram[self.vram_bank][a-0x8000]
        if a < 0xC000: return self.mbc.read(a)
        if a < 0xD000: return self.wram[0][a-0xC000]
        if a < 0xE000: return self.wram[self.wram_bank][a-0xD000]
        if a < 0xFE00: return self.rb(a-0x2000)
        if a < 0xFEA0: return self.oam[a-0xFE00]
        if a < 0xFF00: return 0xFF
        if a < 0xFF80: return self._rio(a & 0x7F)
        if a < 0xFFFF: return self.hram[a-0xFF80]
        return self.ie

    def wb(self, a, v):
        a &= 0xFFFF; v &= 0xFF
        if a < 0x8000: self.mbc.write(a, v); return
        if a < 0xA000: self.vram[self.vram_bank][a-0x8000] = v; return
        if a < 0xC000: self.mbc.write(a, v); return
        if a < 0xD000: self.wram[0][a-0xC000] = v; return
        if a < 0xE000: self.wram[self.wram_bank][a-0xD000] = v; return
        if a < 0xFE00: self.wb(a-0x2000, v); return
        if a < 0xFEA0: self.oam[a-0xFE00] = v; return
        if a < 0xFF00: return
        if a < 0xFF80: self._wio(a & 0x7F, v); return
        if a < 0xFFFF: self.hram[a-0xFF80] = v; return
        self.ie = v

    def _rio(self, r):
        if r == 0x00:   # JOYP
            sel = self.io[0x00] & 0x30
            # bit5=0 (P15 low) → direction; bit4=0 (P14 low) → action
            if   sel == 0x10: return 0x10 | (self.joy_dir & 0x0F) | 0xC0
            elif sel == 0x20: return 0x20 | (self.joy_act & 0x0F) | 0xC0
            elif sel == 0x00: return 0x00 | ((self.joy_dir & self.joy_act) & 0x0F) | 0xC0
            return 0xFF
        if r == 0x04: return (self.div_c >> 8) & 0xFF
        if r == 0x0F: return self.if_ | 0xE0
        if r == 0x41:
            ly = self.io[0x44]; lyc = self.io[0x45]
            cc = 0x04 if ly == lyc else 0
            return (self.io[0x41] & 0xF8) | cc | (self.io[0x41] & 0x03)
        if r == 0x4F: return 0xFE | self.vram_bank
        if r == 0x68: return self.bgpi | (0x80 if self.bgpi_a else 0) | 0x40
        if r == 0x69: return self.bgp[self.bgpi & 0x3F]
        if r == 0x6A: return self.obpi | (0x80 if self.obpi_a else 0) | 0x40
        if r == 0x6B: return self.obp[self.obpi & 0x3F]
        if r == 0x70: return self.wram_bank | 0xF8
        return self.io[r] if r < 0x80 else 0xFF

    def _wio(self, r, v):
        if r == 0x00: self.io[0x00] = (v & 0x30) | 0xCF; return
        if r == 0x04: self.div_c = 0; self.io[0x04] = 0; return
        if r == 0x0F: self.if_ = v & 0x1F; return
        if r == 0x41: self.io[0x41] = (v & 0x78) | (self.io[0x41] & 0x07); return
        if r == 0x44: return          # LY read-only
        if r == 0x46:                 # OAM DMA
            src = v << 8
            for i in range(0xA0): self.oam[i] = self.rb(src + i)
            return
        if r == 0x4D: self.io[0x4D] = (self.io[0x4D] & 0xFE) | (v & 0x01); return
        if r == 0x4F: self.vram_bank = v & 0x01; self.io[0x4F] = 0xFE | self.vram_bank; return
        if r == 0x55: self._hdma(v); return
        if r == 0x68: self.bgpi = v & 0x3F; self.bgpi_a = bool(v & 0x80); return
        if r == 0x69:
            self.bgp[self.bgpi & 0x3F] = v
            if self.bgpi_a: self.bgpi = (self.bgpi + 1) & 0x3F
            return
        if r == 0x6A: self.obpi = v & 0x3F; self.obpi_a = bool(v & 0x80); return
        if r == 0x6B:
            self.obp[self.obpi & 0x3F] = v
            if self.obpi_a: self.obpi = (self.obpi + 1) & 0x3F
            return
        if r == 0x70: self.wram_bank = max(1, v & 0x07); self.io[0x70] = self.wram_bank | 0xF8; return
        if r < 0x80: self.io[r] = v

    def _hdma(self, v):
        n = ((v & 0x7F) + 1) * 16
        src = ((self.io[0x51] << 8) | self.io[0x52]) & 0xFFF0
        dst = (((self.io[0x53] << 8) | self.io[0x54]) & 0x1FF0) | 0x8000
        for i in range(n): self.wb(dst+i, self.rb(src+i))
        self.io[0x55] = 0xFF

    # ── CPU helpers ───────────────────────────────────────────
    def _r8(self):  v=self.rb(self.pc); self.pc=(self.pc+1)&0xFFFF; return v
    def _r16(self):
        lo=self.rb(self.pc); self.pc=(self.pc+1)&0xFFFF
        hi=self.rb(self.pc); self.pc=(self.pc+1)&0xFFFF
        return (hi<<8)|lo
    def _push(self,v):
        self.sp=(self.sp-1)&0xFFFF; self.wb(self.sp,v>>8)
        self.sp=(self.sp-1)&0xFFFF; self.wb(self.sp,v&0xFF)
    def _pop(self):
        lo=self.rb(self.sp); self.sp=(self.sp+1)&0xFFFF
        hi=self.rb(self.sp); self.sp=(self.sp+1)&0xFFFF
        return (hi<<8)|lo
    def _af(self): return (self.a<<8)|self.f
    def _bc(self): return (self.b<<8)|self.c
    def _de(self): return (self.d<<8)|self.e
    def _hl(self): return (self.h<<8)|self.l
    def _saf(self,v): self.a=(v>>8)&0xFF; self.f=v&0xF0
    def _sbc(self,v): self.b=(v>>8)&0xFF; self.c=v&0xFF
    def _sde(self,v): self.d=(v>>8)&0xFF; self.e=v&0xFF
    def _shl(self,v): self.h=(v>>8)&0xFF; self.l=v&0xFF
    def _fz(self): return bool(self.f&0x80)
    def _fn(self): return bool(self.f&0x40)
    def _fh(self): return bool(self.f&0x20)
    def _fc(self): return bool(self.f&0x10)

    def _gr(self,i):
        if i==0: return self.b
        if i==1: return self.c
        if i==2: return self.d
        if i==3: return self.e
        if i==4: return self.h
        if i==5: return self.l
        if i==6: return self.rb(self._hl())
        return self.a
    def _sr(self,i,v):
        v&=0xFF
        if   i==0: self.b=v
        elif i==1: self.c=v
        elif i==2: self.d=v
        elif i==3: self.e=v
        elif i==4: self.h=v
        elif i==5: self.l=v
        elif i==6: self.wb(self._hl(),v)
        else:      self.a=v

    # ── ALU ──────────────────────────────────────────────────
    def _add(self,v,c=0):
        r=self.a+v+c
        self.f=(0x80 if not r&0xFF else 0)|(0x20 if (self.a&0xF)+(v&0xF)+c>0xF else 0)|(0x10 if r>0xFF else 0)
        self.a=r&0xFF
    def _sub(self,v,c=0):
        r=self.a-v-c
        self.f=0x40|(0x80 if not r&0xFF else 0)|(0x20 if (self.a&0xF)-(v&0xF)-c<0 else 0)|(0x10 if r<0 else 0)
        self.a=r&0xFF
    def _and(self,v): self.a&=v; self.f=0x20|(0x80 if not self.a else 0)
    def _or (self,v): self.a|=v; self.f=0x80 if not self.a else 0
    def _xor(self,v): self.a^=v; self.f=0x80 if not self.a else 0
    def _cp (self,v):
        r=self.a-v
        self.f=0x40|(0x80 if not r&0xFF else 0)|(0x20 if (self.a&0xF)-(v&0xF)<0 else 0)|(0x10 if r<0 else 0)
    def _inc(self,v):
        r=(v+1)&0xFF; self.f=(self.f&0x10)|(0x80 if not r else 0)|(0x20 if (v&0xF)==0xF else 0); return r
    def _dec(self,v):
        r=(v-1)&0xFF; self.f=(self.f&0x10)|0x40|(0x80 if not r else 0)|(0x20 if (v&0xF)==0 else 0); return r
    def _addhl(self,v):
        hl=self._hl(); r=hl+v
        self.f=(self.f&0x80)|(0x20 if (hl&0xFFF)+(v&0xFFF)>0xFFF else 0)|(0x10 if r>0xFFFF else 0)
        self._shl(r&0xFFFF)
    def _rl(self,v,t=True):
        oc=1 if self._fc() else 0
        if t: r,c=((v<<1)|oc)&0xFF,bool(v&0x80)
        else: r,c=((v<<1)|(v>>7))&0xFF,bool(v&0x80)
        self.f=(0x10 if c else 0)|(0x80 if not r else 0); return r
    def _rr(self,v,t=True):
        oc=0x80 if self._fc() else 0
        if t: r,c=((v>>1)|oc)&0xFF,bool(v&1)
        else: r,c=((v>>1)|((v&1)<<7))&0xFF,bool(v&1)
        self.f=(0x10 if c else 0)|(0x80 if not r else 0); return r
    def _sla(self,v): c=bool(v&0x80); r=(v<<1)&0xFF; self.f=(0x10 if c else 0)|(0x80 if not r else 0); return r
    def _sra(self,v): c=bool(v&1); r=(v>>1)|(v&0x80); self.f=(0x10 if c else 0)|(0x80 if not r else 0); return r
    def _srl(self,v): c=bool(v&1); r=v>>1; self.f=(0x10 if c else 0)|(0x80 if not r else 0); return r
    def _swap(self,v): r=((v&0xF)<<4)|(v>>4); self.f=0x80 if not r else 0; return r
    def _bit(self,b,v): self.f=(self.f&0x10)|0x20|(0x80 if not(v&(1<<b)) else 0)
    def _addsp(self,r8):
        if r8>127: r8-=256
        r=self.sp+r8
        self.f=(0x20 if (self.sp^r8^r)&0x10 else 0)|(0x10 if (self.sp^r8^r)&0x100 else 0)
        return r&0xFFFF

    # ── CPU step ─────────────────────────────────────────────
    def step(self):
        # Handle HALT
        if self.halted:
            pending = self.if_ & self.ie & 0x1F
            if not pending:
                if self.ime_p: self.ime=True; self.ime_p=False
                return 4
            self.halted = False
        # Service interrupt
        if self.ime:
            pending = self.if_ & self.ie & 0x1F
            if pending:
                self.ime = False
                for b in range(5):
                    if pending & (1<<b):
                        self.if_ &= ~(1<<b)
                        self._push(self.pc)
                        self.pc = [0x40,0x48,0x50,0x58,0x60][b]
                        if self.ime_p: self.ime_p=False
                        return 20
        if self.ime_p: self.ime=True; self.ime_p=False
        return self._exec(self._r8())

    def _exec(self, op):
        # LD r,r range  (40-7F)
        if 0x40 <= op <= 0x7F:
            if op==0x76: self.halted=True; return 4
            d=(op>>3)&7; s=op&7; self._sr(d,self._gr(s))
            return 8 if (d==6 or s==6) else 4
        # ALU r range   (80-BF)
        if 0x80 <= op <= 0xBF:
            v=self._gr(op&7); cy=8 if (op&7)==6 else 4; k=(op>>3)&7
            if   k==0: self._add(v)
            elif k==1: self._add(v,1 if self._fc() else 0)
            elif k==2: self._sub(v)
            elif k==3: self._sub(v,1 if self._fc() else 0)
            elif k==4: self._and(v)
            elif k==5: self._xor(v)
            elif k==6: self._or(v)
            else:      self._cp(v)
            return cy
        # Everything else
        if op==0x00: return 4
        if op==0x01: self._sbc(self._r16()); return 12
        if op==0x02: self.wb(self._bc(),self.a); return 8
        if op==0x03: self._sbc((self._bc()+1)&0xFFFF); return 8
        if op==0x04: self.b=self._inc(self.b); return 4
        if op==0x05: self.b=self._dec(self.b); return 4
        if op==0x06: self.b=self._r8(); return 8
        if op==0x07:
            c=(self.a>>7)&1; self.a=((self.a<<1)|c)&0xFF; self.f=0x10 if c else 0; return 4
        if op==0x08:
            a=self._r16(); self.wb(a,self.sp&0xFF); self.wb(a+1,self.sp>>8); return 20
        if op==0x09: self._addhl(self._bc()); return 8
        if op==0x0A: self.a=self.rb(self._bc()); return 8
        if op==0x0B: self._sbc((self._bc()-1)&0xFFFF); return 8
        if op==0x0C: self.c=self._inc(self.c); return 4
        if op==0x0D: self.c=self._dec(self.c); return 4
        if op==0x0E: self.c=self._r8(); return 8
        if op==0x0F:
            c=self.a&1; self.a=((self.a>>1)|(c<<7))&0xFF; self.f=0x10 if c else 0; return 4
        if op==0x10: self._r8(); return 4
        if op==0x11: self._sde(self._r16()); return 12
        if op==0x12: self.wb(self._de(),self.a); return 8
        if op==0x13: self._sde((self._de()+1)&0xFFFF); return 8
        if op==0x14: self.d=self._inc(self.d); return 4
        if op==0x15: self.d=self._dec(self.d); return 4
        if op==0x16: self.d=self._r8(); return 8
        if op==0x17:
            oc=1 if self._fc() else 0; c=(self.a>>7)&1
            self.a=((self.a<<1)|oc)&0xFF; self.f=0x10 if c else 0; return 4
        if op==0x18:
            o=self._r8(); o=o-256 if o>127 else o; self.pc=(self.pc+o)&0xFFFF; return 12
        if op==0x19: self._addhl(self._de()); return 8
        if op==0x1A: self.a=self.rb(self._de()); return 8
        if op==0x1B: self._sde((self._de()-1)&0xFFFF); return 8
        if op==0x1C: self.e=self._inc(self.e); return 4
        if op==0x1D: self.e=self._dec(self.e); return 4
        if op==0x1E: self.e=self._r8(); return 8
        if op==0x1F:
            oc=0x80 if self._fc() else 0; c=self.a&1
            self.a=((self.a>>1)|oc)&0xFF; self.f=0x10 if c else 0; return 4
        if op==0x20:
            o=self._r8(); o=o-256 if o>127 else o
            if not self._fz(): self.pc=(self.pc+o)&0xFFFF; return 12
            return 8
        if op==0x21: self._shl(self._r16()); return 12
        if op==0x22: self.wb(self._hl(),self.a); self._shl((self._hl()+1)&0xFFFF); return 8
        if op==0x23: self._shl((self._hl()+1)&0xFFFF); return 8
        if op==0x24: self.h=self._inc(self.h); return 4
        if op==0x25: self.h=self._dec(self.h); return 4
        if op==0x26: self.h=self._r8(); return 8
        if op==0x27:  # DAA
            a=self.a
            if not self._fn():
                if self._fh() or (a&0xF)>9: a+=0x06
                if self._fc() or a>0x9F: a+=0x60
            else:
                if self._fh(): a-=0x06
                if self._fc(): a-=0x60
            self.f&=0x40
            if a>0xFF or a<0: self.f|=0x10
            self.a=a&0xFF
            if not self.a: self.f|=0x80
            return 4
        if op==0x28:
            o=self._r8(); o=o-256 if o>127 else o
            if self._fz(): self.pc=(self.pc+o)&0xFFFF; return 12
            return 8
        if op==0x29: self._addhl(self._hl()); return 8
        if op==0x2A: self.a=self.rb(self._hl()); self._shl((self._hl()+1)&0xFFFF); return 8
        if op==0x2B: self._shl((self._hl()-1)&0xFFFF); return 8
        if op==0x2C: self.l=self._inc(self.l); return 4
        if op==0x2D: self.l=self._dec(self.l); return 4
        if op==0x2E: self.l=self._r8(); return 8
        if op==0x2F: self.a^=0xFF; self.f|=0x60; return 4
        if op==0x30:
            o=self._r8(); o=o-256 if o>127 else o
            if not self._fc(): self.pc=(self.pc+o)&0xFFFF; return 12
            return 8
        if op==0x31: self.sp=self._r16(); return 12
        if op==0x32: self.wb(self._hl(),self.a); self._shl((self._hl()-1)&0xFFFF); return 8
        if op==0x33: self.sp=(self.sp+1)&0xFFFF; return 8
        if op==0x34: v=self.rb(self._hl()); self.wb(self._hl(),self._inc(v)); return 12
        if op==0x35: v=self.rb(self._hl()); self.wb(self._hl(),self._dec(v)); return 12
        if op==0x36: self.wb(self._hl(),self._r8()); return 12
        if op==0x37: self.f=(self.f&0x80)|0x10; return 4
        if op==0x38:
            o=self._r8(); o=o-256 if o>127 else o
            if self._fc(): self.pc=(self.pc+o)&0xFFFF; return 12
            return 8
        if op==0x39: self._addhl(self.sp); return 8
        if op==0x3A: self.a=self.rb(self._hl()); self._shl((self._hl()-1)&0xFFFF); return 8
        if op==0x3B: self.sp=(self.sp-1)&0xFFFF; return 8
        if op==0x3C: self.a=self._inc(self.a); return 4
        if op==0x3D: self.a=self._dec(self.a); return 4
        if op==0x3E: self.a=self._r8(); return 8
        if op==0x3F: self.f=(self.f&0x80)|(0 if self._fc() else 0x10); return 4
        # --- C0-FF ---
        if op==0xC0:
            if not self._fz(): self.pc=self._pop(); return 20
            return 8
        if op==0xC1: self._sbc(self._pop()); return 12
        if op==0xC2:
            a=self._r16()
            if not self._fz(): self.pc=a; return 16
            return 12
        if op==0xC3: self.pc=self._r16(); return 16
        if op==0xC4:
            a=self._r16()
            if not self._fz(): self._push(self.pc); self.pc=a; return 24
            return 12
        if op==0xC5: self._push(self._bc()); return 16
        if op==0xC6: self._add(self._r8()); return 8
        if op==0xC7: self._push(self.pc); self.pc=0x00; return 16
        if op==0xC8:
            if self._fz(): self.pc=self._pop(); return 20
            return 8
        if op==0xC9: self.pc=self._pop(); return 16
        if op==0xCA:
            a=self._r16()
            if self._fz(): self.pc=a; return 16
            return 12
        if op==0xCB: return self._exec_cb()
        if op==0xCC:
            a=self._r16()
            if self._fz(): self._push(self.pc); self.pc=a; return 24
            return 12
        if op==0xCD: a=self._r16(); self._push(self.pc); self.pc=a; return 24
        if op==0xCE: self._add(self._r8(),1 if self._fc() else 0); return 8
        if op==0xCF: self._push(self.pc); self.pc=0x08; return 16
        if op==0xD0:
            if not self._fc(): self.pc=self._pop(); return 20
            return 8
        if op==0xD1: self._sde(self._pop()); return 12
        if op==0xD2:
            a=self._r16()
            if not self._fc(): self.pc=a; return 16
            return 12
        if op==0xD4:
            a=self._r16()
            if not self._fc(): self._push(self.pc); self.pc=a; return 24
            return 12
        if op==0xD5: self._push(self._de()); return 16
        if op==0xD6: self._sub(self._r8()); return 8
        if op==0xD7: self._push(self.pc); self.pc=0x10; return 16
        if op==0xD8:
            if self._fc(): self.pc=self._pop(); return 20
            return 8
        if op==0xD9: self.pc=self._pop(); self.ime=True; return 16
        if op==0xDA:
            a=self._r16()
            if self._fc(): self.pc=a; return 16
            return 12
        if op==0xDC:
            a=self._r16()
            if self._fc(): self._push(self.pc); self.pc=a; return 24
            return 12
        if op==0xDE: self._sub(self._r8(),1 if self._fc() else 0); return 8
        if op==0xDF: self._push(self.pc); self.pc=0x18; return 16
        if op==0xE0: self.wb(0xFF00|self._r8(),self.a); return 12
        if op==0xE1: self._shl(self._pop()); return 12
        if op==0xE2: self.wb(0xFF00|self.c,self.a); return 8
        if op==0xE5: self._push(self._hl()); return 16
        if op==0xE6: self._and(self._r8()); return 8
        if op==0xE7: self._push(self.pc); self.pc=0x20; return 16
        if op==0xE8: self.sp=self._addsp(self._r8()); return 16
        if op==0xE9: self.pc=self._hl(); return 4
        if op==0xEA: self.wb(self._r16(),self.a); return 16
        if op==0xEE: self._xor(self._r8()); return 8
        if op==0xEF: self._push(self.pc); self.pc=0x28; return 16
        if op==0xF0: self.a=self.rb(0xFF00|self._r8()); return 12
        if op==0xF1: self._saf(self._pop()); return 12
        if op==0xF2: self.a=self.rb(0xFF00|self.c); return 8
        if op==0xF3: self.ime=False; self.ime_p=False; return 4
        if op==0xF5: self._push(self._af()); return 16
        if op==0xF6: self._or(self._r8()); return 8
        if op==0xF7: self._push(self.pc); self.pc=0x30; return 16
        if op==0xF8: self._shl(self._addsp(self._r8())); return 12
        if op==0xF9: self.sp=self._hl(); return 8
        if op==0xFA: self.a=self.rb(self._r16()); return 16
        if op==0xFB: self.ime_p=True; return 4
        if op==0xFE: self._cp(self._r8()); return 8
        if op==0xFF: self._push(self.pc); self.pc=0x38; return 16
        return 4    # undefined opcode: NOP

    def _exec_cb(self):
        op=self._r8(); r=op&7; v=self._gr(r)
        k=(op>>6)&3; b=(op>>3)&7
        if k==0:
            if   b==0: res=self._rl(v,t=False)
            elif b==1: res=self._rr(v,t=False)
            elif b==2: res=self._rl(v,t=True)
            elif b==3: res=self._rr(v,t=True)
            elif b==4: res=self._sla(v)
            elif b==5: res=self._sra(v)
            elif b==6: res=self._swap(v)
            else:      res=self._srl(v)
            self._sr(r,res); return 16 if r==6 else 8
        elif k==1:
            self._bit(b,v); return 12 if r==6 else 8
        elif k==2:
            self._sr(r,v&~(1<<b)); return 16 if r==6 else 8
        else:
            self._sr(r,v|(1<<b));  return 16 if r==6 else 8

    # ── PPU: render one scanline ──────────────────────────────
    def _render(self, ly):
        lcdc = self.io[0x40]
        buf  = self.fbuf
        base = ly * GBC_W * 2
        if not (lcdc & 0x80):
            for x in range(GBC_W*2): buf[base+x]=0xFF
            return

        scy=self.io[0x42]; scx=self.io[0x43]
        wx=self.io[0x4B]-7; wy=self.io[0x4A]
        v0=self.vram[0]; v1=self.vram[1]

        # Per-pixel state
        ci=[0]*GBC_W; pi=[0]*GBC_W; bp=[False]*GBC_W

        tb=0x0000 if lcdc&0x10 else 0x1000
        st=not(lcdc&0x10)

        # Background
        if lcdc & 0x01:
            bm=0x1C00 if lcdc&0x08 else 0x1800
            my=(ly+scy)&0xFF; ty=my>>3; py=my&7
            for x in range(GBC_W):
                mx=(x+scx)&0xFF; tx=mx>>3; px=mx&7
                i=bm+ty*32+tx
                tile=v0[i]; attr=v1[i]
                vb=(attr>>3)&1
                py2=7-py if attr&0x40 else py
                px2=7-px if attr&0x20 else px
                if st:
                    t=tile if tile<128 else tile-256; ta=(tb+t*16)&0x1FFF
                else: ta=(tile*16)&0x1FFF
                lo=self.vram[vb][ta+py2*2]; hi=self.vram[vb][ta+py2*2+1]
                bit=7-px2; c=((hi>>bit)&1)<<1|((lo>>bit)&1)
                ci[x]=c; pi[x]=attr&7; bp[x]=bool(attr&0x80)

        # Window
        if (lcdc&0x20) and wy<=ly and wx<GBC_W:
            wm=0x1C00 if lcdc&0x40 else 0x1800
            wly=self.wlc; ty=wly>>3; py=wly&7
            for x in range(max(0,wx),GBC_W):
                wx2=x-wx; tx=wx2>>3; px=wx2&7
                i=wm+ty*32+tx
                tile=v0[i]; attr=v1[i]
                vb=(attr>>3)&1
                py2=7-py if attr&0x40 else py
                px2=7-px if attr&0x20 else px
                if st:
                    t=tile if tile<128 else tile-256; ta=(tb+t*16)&0x1FFF
                else: ta=(tile*16)&0x1FFF
                lo=self.vram[vb][ta+py2*2]; hi=self.vram[vb][ta+py2*2+1]
                bit=7-px2; c=((hi>>bit)&1)<<1|((lo>>bit)&1)
                ci[x]=c; pi[x]=attr&7; bp[x]=bool(attr&0x80)
            self.wlc+=1

        # Sprites
        sh=16 if lcdc&0x04 else 8
        oc={}
        if lcdc&0x02:
            spr=[]
            for i in range(40):
                sy=self.oam[i*4]-16; sx=self.oam[i*4+1]-8
                if ly>=sy and ly<sy+sh:
                    spr.append((sx,sy,self.oam[i*4+2],self.oam[i*4+3]))
                    if len(spr)==10: break
            # GBC priority: OAM order (sprite 0 = highest). No X sort.
            # Process reversed so sprite 0 is drawn last (on top).
            for sx,sy,tile,fl in reversed(spr):
                vb=(fl>>3)&1; fx=bool(fl&0x20); fy=bool(fl&0x40)
                bh=bool(fl&0x80); opal=fl&7
                if sh==16: tile&=0xFE
                row=ly-sy
                if fy: row=sh-1-row
                ta=tile*16+row*2
                lo=self.vram[vb][ta]; hi=self.vram[vb][ta+1]
                for px in range(8):
                    sx2=sx+px
                    if sx2<0 or sx2>=GBC_W: continue
                    bit=7-(7-px if fx else px)
                    c=((hi>>bit)&1)<<1|((lo>>bit)&1)
                    if c==0: continue
                    oc[sx2]=(c,opal,bh)

        # → RGB565
        bgp=self.bgp; obp=self.obp
        for x in range(GBC_W):
            if x in oc:
                c,opal,bh=oc[x]
                if not bh or ci[x]==0:
                    pi2=(opal*4+c)*2
                    lo=obp[pi2]; hi=obp[pi2+1]
                    r5=lo&0x1F; g5=((hi&3)<<3)|(lo>>5); b5=(hi>>2)&0x1F
                    g6=(g5<<1)|(g5>>4); px16=(r5<<11)|(g6<<5)|b5
                    buf[base+x*2]=px16&0xFF; buf[base+x*2+1]=px16>>8
                    continue
            pi2=(pi[x]*4+ci[x])*2
            lo=bgp[pi2]; hi=bgp[pi2+1]
            r5=lo&0x1F; g5=((hi&3)<<3)|(lo>>5); b5=(hi>>2)&0x1F
            g6=(g5<<1)|(g5>>4); px16=(r5<<11)|(g6<<5)|b5
            buf[base+x*2]=px16&0xFF; buf[base+x*2+1]=px16>>8

    # ── PPU tick ──────────────────────────────────────────────
    def _ppu(self, cyc):
        if not (self.io[0x40]&0x80): return False
        self.scyc+=cyc; ly=self.io[0x44]
        if ly<144:
            mode=2 if self.scyc<80 else 3 if self.scyc<252 else 0
        else: mode=1
        pm=self.io[0x41]&3; self.io[0x41]=(self.io[0x41]&0xFC)|mode
        stat=self.io[0x41]
        if mode!=pm:
            if (mode==0 and stat&0x08)or(mode==1 and stat&0x10)or(mode==2 and stat&0x20):
                self.if_|=0x02
        vbl=False
        if self.scyc>=456:
            self.scyc-=456
            if ly<144 and self._draw: self._render(ly)
            ly=(ly+1)%154; self.io[0x44]=ly
            if ly==144: self.if_|=0x01; vbl=True; self.wlc=0
            elif ly==0: self.wlc=0
            lyc=self.io[0x45]
            if ly==lyc:
                self.io[0x41]|=0x04
                if stat&0x40: self.if_|=0x02
            else: self.io[0x41]&=~0x04
        return vbl

    # ── Timer tick ────────────────────────────────────────────
    def _timer(self, cyc):
        self.div_c=(self.div_c+cyc)&0xFFFF; self.io[0x04]=(self.div_c>>8)&0xFF
        tac=self.io[0x07]
        if tac&0x04:
            self.tima_c+=cyc; freq=[1024,16,64,256][tac&3]
            while self.tima_c>=freq:
                self.tima_c-=freq
                t=(self.io[0x05]+1)&0xFF; self.io[0x05]=t
                if t==0: self.io[0x05]=self.io[0x06]; self.if_|=0x04

    # ── Run one frame ─────────────────────────────────────────
    def run_frame(self, draw=True):
        """draw=False skips pixel rendering (PPU timing still runs)."""
        self._draw = draw
        done=0
        while done<CYCLES_PER_FRAME:
            c=self.step(); self._timer(c); self._ppu(c); done+=c

    # ── Joypad ────────────────────────────────────────────────
    def set_keys(self, bits):
        """Map pager button bitmask to GBC joypad."""
        # Combo: A+RIGHT = START,  A+LEFT = SELECT
        start  = bool((bits & P_A) and (bits & P_R))
        select = bool((bits & P_A) and (bits & P_L))

        act = 0xFF
        if bits & P_A:               act &= ~0x01   # A
        if bits & P_B:               act &= ~0x02   # B
        if select:                   act &= ~0x04   # SELECT
        if start:                    act &= ~0x08   # START
        self.joy_act = act

        dir_ = 0xFF
        if bits & P_R and not start:  dir_ &= ~0x01  # RIGHT
        if bits & P_L and not select: dir_ &= ~0x02  # LEFT
        if bits & P_UP:               dir_ &= ~0x04  # UP
        if bits & P_DN:               dir_ &= ~0x08  # DOWN
        self.joy_dir = dir_

        if bits: self.if_ |= 0x10    # joypad IRQ


# ──────────────────────────────────────────────────────────────
# BLIT: GBC frame → pager framebuffer  (1.5× nearest-neighbour)
# ──────────────────────────────────────────────────────────────
_pbuf = bytearray(FB_STRIDE * PAGER_H)   # sized using stride; reuse each frame

def blit(gbc_frame):
    prev_gy = -1
    prev_row_start = 0
    for oy in range(DISP_H):
        gy = SCALE_Y[oy]; gr = gy * GBC_W * 2
        dr = (OFF_Y + oy) * FB_STRIDE + OFF_X * 2
        if gy == prev_gy:
            # Same GBC row as previous display row — copy it (fast C-level memcpy)
            _pbuf[dr:dr+DISP_W*2] = _pbuf[prev_row_start:prev_row_start+DISP_W*2]
        else:
            for ox in range(DISP_W):
                s = gr + SCALE_X[ox]*2; d = dr + ox*2
                _pbuf[d]=gbc_frame[s]; _pbuf[d+1]=gbc_frame[s+1]
            prev_gy = gy; prev_row_start = dr


# ──────────────────────────────────────────────────────────────
# FB0 STRIDE PROBE
# ──────────────────────────────────────────────────────────────
def _probe_fb_stride():
    """Return the actual bytes-per-row of /dev/fb0 via FBIOGET_FSCREENINFO ioctl."""
    import struct, fcntl
    try:
        buf = bytearray(80)
        with open('/dev/fb0', 'rb') as fd:
            fcntl.ioctl(fd, 0x4602, buf)   # FBIOGET_FSCREENINFO
        # fb_fix_screeninfo on 32-bit MIPS (4-byte unsigned long):
        # id[16] + smem_start(4) + smem_len(4) + type(4) + type_aux(4) + visual(4)
        # + xpanstep(2) + ypanstep(2) + ywrapstep(2) + pad(2) → line_length at offset 44
        stride = struct.unpack_from('<I', buf, 44)[0]
        if stride >= PAGER_W * 2:
            return stride
    except Exception as e:
        _log(f"[zelda] fb stride probe failed: {e}")
    return PAGER_W * 2


# ──────────────────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────────────────
def main():
    import traceback
    _dir = os.path.dirname(os.path.abspath(__file__))
    rom  = os.path.join(_dir, 'zelda.gbc')

    _log(f"[zelda] dir={_dir}  python={sys.version.split()[0]}")

    if not os.path.exists(rom):
        _log(f"[zelda] ERROR: ROM not found: {rom}"); sys.exit(1)
    _log(f"[zelda] ROM ok, size={os.path.getsize(rom)}")
    _log(f"[zelda] libpagerctl.so: {os.path.exists(os.path.join(_dir,'libpagerctl.so'))}")
    _log(f"[zelda] /dev/fb0: {os.path.exists('/dev/fb0')}")

    # Init display
    pager = None
    try:
        sys.path.insert(0, _dir)
        _log("[zelda] importing pagerctl...")
        from pagerctl import Pager
        pager = Pager()
        _log("[zelda] calling pager.init()...")
        ret = pager.init()
        _log(f"[zelda] pager.init() = {ret}")
        pager.set_rotation(270)
        pager.clear(0x0000)
        pager.draw_text(10, 80,  "Zelda: Link's Awakening DX", 0xFFFF, 1)
        pager.draw_text(10, 100, "Loading...",                  0x07E0, 1)
        pager.draw_text(10, 120, "A+RIGHT=START  A+LEFT=SELECT",0xFFE0, 1)
        pager.draw_text(10, 140, "Hold A+B (2s) to quit",       0xF800, 1)
        pager.flip()
        _log("[zelda] splash shown")
    except Exception as e:
        _log(f"[zelda] pagerctl error: {e}")
        traceback.print_exc(_LOG)

    gb = GameBoy(rom)
    ft = 1.0 / 59.73

    # Probe and apply actual fb0 row stride before opening
    global FB_STRIDE, _pbuf
    FB_STRIDE = _probe_fb_stride()
    if FB_STRIDE != PAGER_W * 2:
        _pbuf = bytearray(FB_STRIDE * PAGER_H)
        _log(f"[zelda] fb_stride={FB_STRIDE} (corrected from {PAGER_W*2})")
    else:
        _log(f"[zelda] fb_stride={FB_STRIDE} (standard)")

    # Open framebuffer once and keep it open — opening per-frame is too slow on MIPS
    fb0 = None
    if os.path.exists('/dev/fb0'):
        try:
            fb0 = open('/dev/fb0', 'wb', buffering=0)
            _log("[zelda] /dev/fb0 opened")
        except Exception as e:
            _log(f"[zelda] /dev/fb0 open failed: {e}")
    t_next = time.monotonic() + ft

    use_fb0 = fb0 is not None
    prev_btn    = 0
    ab_held     = None   # time when A+B combo started
    skip_cnt    = 0      # consecutive skipped frames
    MAX_SKIP    = 20     # skip up to 20 frames so the CPU runs at full speed
    frame_n     = 0

    try:
        while True:
            # ── Poll input ──────────────────────────────────
            if pager:
                try:
                    cur, _, _ = pager.poll_input()
                    if cur != prev_btn:
                        gb.set_keys(cur)
                        prev_btn = cur
                    # A+B held 2 s = quit
                    if (cur & (P_A | P_B)) == (P_A | P_B):
                        if ab_held is None: ab_held = time.monotonic()
                        elif time.monotonic() - ab_held >= 2.0: break
                    else:
                        ab_held = None
                except Exception:
                    pass

            # ── Adaptive frame-skip ─────────────────────────
            # If we are behind schedule, skip rendering this frame
            # (CPU/timer/PPU timing still run for correctness)
            now = time.monotonic()
            behind = now > t_next
            draw_this = not (behind and skip_cnt < MAX_SKIP)
            if not draw_this: skip_cnt += 1
            else: skip_cnt = 0

            gb.run_frame(draw=draw_this)
            frame_n += 1

            # ── Display frame ───────────────────────────────
            if draw_this:
                blit(gb.fbuf)
                if use_fb0:
                    try:
                        fb0.seek(0); fb0.write(_pbuf)
                    except Exception:
                        pass
                elif pager:
                    try:
                        for oy in range(DISP_H):
                            dr = (OFF_Y+oy)*PAGER_W + OFF_X
                            for ox in range(DISP_W):
                                i=(dr+ox)*2; c=_pbuf[i]|(_pbuf[i+1]<<8)
                                pager.pixel(OFF_X+ox, OFF_Y+oy, c)
                        pager.flip()
                    except Exception:
                        pass

            # ── Throttle (only when ahead of schedule) ──────
            now = time.monotonic()
            if now < t_next:
                time.sleep(t_next - now)
            t_next = max(t_next + ft, time.monotonic() - ft * MAX_SKIP)

    except KeyboardInterrupt:
        pass
    finally:
        if fb0:
            try: fb0.close()
            except Exception: pass
        if pager:
            try:
                pager.clear(0x0000); pager.flip(); pager.cleanup()
            except Exception:
                pass
        _log("[zelda] exited")


if __name__ == '__main__':
    main()
