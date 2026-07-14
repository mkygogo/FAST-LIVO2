#!/usr/bin/env python3
"""Convert binary PCD (x y z rgb) to binary little-endian PLY.

Usage:
    python3 pcd_to_ply.py input.pcd [output.ply]

If output is omitted, writes to the same directory with .ply extension.
Supports FIELDS: x y z rgb (binary_little_endian PCD only).
"""
import struct
import sys
import pathlib


def parse_pcd_header(f):
    header = {}
    while True:
        line = f.readline()
        if not line:
            raise ValueError("Unexpected end of file in PCD header")
        text = line.decode("ascii", errors="replace").strip()
        if text.upper().startswith("DATA"):
            header["data_format"] = text.split()[1].lower() if len(text.split()) > 1 else ""
            break
        parts = text.split(None, 1)
        if len(parts) == 2:
            header[parts[0].upper()] = parts[1]
    return header, f.tell()


def convert(input_path, output_path=None):
    input_path = pathlib.Path(input_path)
    if output_path is None:
        output_path = input_path.with_suffix(".ply")
    else:
        output_path = pathlib.Path(output_path)

    with open(input_path, "rb") as f:
        header, data_offset = parse_pcd_header(f)

        fields = header.get("FIELDS", "").split()
        sizes = list(map(int, header.get("SIZE", "").split()))
        types = header.get("TYPE", "").split()
        points = int(header.get("POINTS", header.get("WIDTH", "0")))

        if header.get("data_format") != "binary":
            raise ValueError(f"Only binary PCD is supported, got: {header.get('data_format')}")

        # Build field map
        stride = sum(sizes)
        field_offsets = {}
        offset = 0
        for i, name in enumerate(fields):
            field_offsets[name] = (offset, sizes[i], types[i])
            offset += sizes[i]

        if "x" not in field_offsets or "y" not in field_offsets or "z" not in field_offsets:
            raise ValueError(f"PCD must have x, y, z fields, got: {fields}")

        has_rgb = "rgb" in field_offsets or "rgba" in field_offsets
        rgb_key = "rgb" if "rgb" in field_offsets else ("rgba" if "rgba" in field_offsets else None)

        print(f"Input:  {input_path} ({points} points, stride={stride})")
        print(f"Fields: {fields}, has_rgb={has_rgb}")

        # Read all point data
        f.seek(data_offset)
        raw = f.read(points * stride)

    if len(raw) < points * stride:
        actual = len(raw) // stride
        print(f"Warning: expected {points} points but only got {actual}")
        points = actual

    # Write binary PLY
    ply_header = (
        "ply\n"
        "format binary_little_endian 1.0\n"
        f"element vertex {points}\n"
        "property float x\n"
        "property float y\n"
        "property float z\n"
    )
    if has_rgb:
        ply_header += (
            "property uchar red\n"
            "property uchar green\n"
            "property uchar blue\n"
        )
    ply_header += "end_header\n"

    x_off = field_offsets["x"][0]
    y_off = field_offsets["y"][0]
    z_off = field_offsets["z"][0]
    rgb_off = field_offsets[rgb_key][0] if has_rgb else 0

    with open(output_path, "wb") as out:
        out.write(ply_header.encode("ascii"))
        for i in range(points):
            base = i * stride
            x = struct.unpack_from("<f", raw, base + x_off)[0]
            y = struct.unpack_from("<f", raw, base + y_off)[0]
            z = struct.unpack_from("<f", raw, base + z_off)[0]
            if has_rgb:
                rgb_uint = struct.unpack_from("<I", raw, base + rgb_off)[0]
                r = (rgb_uint >> 16) & 0xFF
                g = (rgb_uint >> 8) & 0xFF
                b = rgb_uint & 0xFF
                out.write(struct.pack("<fff", x, y, z))
                out.write(struct.pack("BBB", r, g, b))
            else:
                out.write(struct.pack("<fff", x, y, z))

    out_size = output_path.stat().st_size
    print(f"Output: {output_path} ({out_size / 1024 / 1024:.1f} MB)")
    print("Done.")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} input.pcd [output.ply]")
        sys.exit(1)
    out = sys.argv[2] if len(sys.argv) > 2 else None
    convert(sys.argv[1], out)
