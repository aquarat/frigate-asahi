#!/usr/bin/env python3
"""Minimal Annex-B H.264 analyser: per clip print SPS/PPS key flags, slice/frame stats."""
import sys, os

class BR:
    def __init__(self, data):
        # strip emulation prevention
        out = bytearray(); i = 0; n = len(data)
        while i < n:
            if i + 2 < n and data[i] == 0 and data[i+1] == 0 and data[i+2] == 3:
                out += b'\x00\x00'; i += 3
            else:
                out.append(data[i]); i += 1
        self.d = bytes(out); self.pos = 0
    def u(self, n):
        v = 0
        for _ in range(n):
            byte = self.d[self.pos >> 3]; bit = (byte >> (7 - (self.pos & 7))) & 1
            v = (v << 1) | bit; self.pos += 1
        return v
    def ue(self):
        lz = 0
        while self.u(1) == 0: lz += 1
        return (1 << lz) - 1 + self.u(lz)
    def se(self):
        k = self.ue()
        return (k + 1) // 2 if k & 1 else -(k // 2)
    def more_rbsp_data(self):
        # find last 1 bit (rbsp_stop_one_bit)
        for i in range(len(self.d) - 1, -1, -1):
            if self.d[i]:
                b = self.d[i]; last = i * 8 + 7 - ((b & -b).bit_length() - 1)
                return self.pos < last
        return False

def nals(data):
    i = 0; n = len(data); starts = []
    while i < n - 3:
        if data[i] == 0 and data[i+1] == 0 and data[i+2] == 1:
            starts.append(i + 3); i += 3
        else: i += 1
    for k, s in enumerate(starts):
        e = starts[k+1] - 3 if k + 1 < len(starts) else n
        while e > s and data[e-1] == 0: e -= 1
        yield data[s:e]

def parse_sps(b):
    br = BR(b[1:]); p = {}
    p['profile'] = br.u(8); br.u(8); p['level'] = br.u(8); br.ue()
    p['chroma'] = 1; p['scaling_sps'] = 0
    if p['profile'] in (100,110,122,244,44,83,86,118,128,138,139,134,135):
        p['chroma'] = br.ue()
        if p['chroma'] == 3: br.u(1)
        br.ue(); br.ue(); br.u(1)
        p['scaling_sps'] = br.u(1)
        if p['scaling_sps']:
            for i in range(8 if p['chroma'] != 3 else 12):
                if br.u(1):
                    sz = 16 if i < 6 else 64; last = 8; nxt = 8
                    for j in range(sz):
                        if nxt: nxt = (last + br.se() + 256) % 256
                        last = last if nxt == 0 else nxt
    p['log2_max_frame_num'] = br.ue() + 4
    poc = br.ue(); p['poc_type'] = poc
    if poc == 0: br.ue()
    elif poc == 1:
        br.u(1); br.se(); br.se()
        for _ in range(br.ue()): br.se()
    p['max_num_ref_frames'] = br.ue(); br.u(1)
    p['w_mbs'] = br.ue() + 1; p['h_map'] = br.ue() + 1
    p['frame_mbs_only'] = br.u(1)
    if not p['frame_mbs_only']: br.u(1)
    p['direct_8x8_inference'] = br.u(1)
    return p

def parse_pps(b, chroma=1):
    br = BR(b[1:]); p = {}
    br.ue(); br.ue(); p['cabac'] = br.u(1); br.u(1); p['slice_groups'] = br.ue() + 1
    if p['slice_groups'] > 1:
        t = br.ue()
        if t == 0:
            for _ in range(p['slice_groups']): br.ue()
        elif t == 2:
            for _ in range(p['slice_groups'] - 1): br.ue(); br.ue()
        elif t in (3,4,5): br.u(1); br.ue()
        elif t == 6:
            n = br.ue()
            import math
            for _ in range(n): br.u(max(1, math.ceil(math.log2(p['slice_groups']))))
    p['num_ref_idx_l0'] = br.ue() + 1; p['num_ref_idx_l1'] = br.ue() + 1
    p['weighted_pred'] = br.u(1); p['weighted_bipred_idc'] = br.u(2)
    p['init_qp'] = 26 + br.se(); br.se(); p['cqo'] = br.se(); p['dbf_ctrl'] = br.u(1); p['cip'] = br.u(1); br.u(1)
    p['transform_8x8'] = 0; p['scaling_pps'] = 0; p['pps_lists_present'] = ''; p['second_cqo'] = p['cqo']
    if br.more_rbsp_data():
        p['transform_8x8'] = br.u(1); p['scaling_pps'] = br.u(1)
        if p['scaling_pps']:
            n = 6 + (6 if chroma == 3 else 2) * p['transform_8x8']
            for i in range(n):
                pres = br.u(1); p['pps_lists_present'] += '1' if pres else '0'
                if pres:
                    sz = 16 if i < 6 else 64; last = 8; nxt = 8
                    for j in range(sz):
                        if nxt: nxt = (last + br.se() + 256) % 256
                        last = last if nxt == 0 else nxt
        p['second_cqo'] = br.se()
    return p

def cut(path, n, out):
    data = open(path, 'rb').read(); frames = 0; ob = bytearray(); cur_started = False
    for nal in nals(data):
        t = nal[0] & 0x1f
        if t in (1, 5):
            br = BR(nal[1:9]); first_mb = br.ue()
            if first_mb == 0:
                frames += 1
                if frames > n: break
        elif frames >= n and t not in (6,):
            if frames > 0: break
        ob += b'\x00\x00\x00\x01' + nal
    open(out, 'wb').write(ob); print(f"wrote {out}: {frames if frames <= n else n} frames, {len(ob)} bytes")

def main(paths):
    if paths and paths[0] == '--cut':
        n = int(paths[1]); cut(paths[2], n, paths[3]); return
    for path in paths:
        data = open(path, 'rb').read()
        sps = pps = None; frames = []; cur = None
        for nal in nals(data):
            t = nal[0] & 0x1f
            if t == 7: sps = parse_sps(nal)
            elif t == 8: pps = parse_pps(nal, sps['chroma'] if sps else 1)
            elif t in (1, 5):
                br = BR(nal[1:9]); first_mb = br.ue(); st = br.ue() % 5
                if first_mb == 0:
                    cur = {'idr': t == 5, 'type': 'PBI'[st] if st < 3 else str(st), 'slices': 0, 'bytes': 0, 'max_slice': 0}
                    frames.append(cur)
                if cur is not None:
                    cur['slices'] += 1; cur['bytes'] += len(nal); cur['max_slice'] = max(cur['max_slice'], len(nal))
        if not sps: print(f"{os.path.basename(path)}: no SPS"); continue
        w = sps['w_mbs'] * 16; h = sps['h_map'] * 16 * (2 - sps['frame_mbs_only'])
        f0 = frames[0] if frames else {}
        maxb = max(f['bytes'] for f in frames) if frames else 0
        maxsl = max(f['max_slice'] for f in frames) if frames else 0
        maxslices = max(f['slices'] for f in frames) if frames else 0
        types = ''.join(f['type'] for f in frames[:12])
        print(f"{os.path.basename(path):26s} {w}x{h} prof={sps['profile']} lvl={sps['level']} cabac={pps['cabac']} 8x8={pps['transform_8x8']} scl={sps['scaling_sps']}/{pps['scaling_pps']}{('['+pps['pps_lists_present']+']') if pps['scaling_pps'] else ''} refs={sps['max_num_ref_frames']} nref_l0={pps['num_ref_idx_l0']} wp={pps['weighted_pred']}/{pps['weighted_bipred_idc']} cqo={pps['cqo']}/{pps['second_cqo']} qp={pps['init_qp']} dbf={pps['dbf_ctrl']} cip={pps['cip']} frames={len(frames)} slices/frame={maxslices} firstIDR={f0.get('bytes',0)} maxframe={maxb} types={types}")

main(sys.argv[1:])
