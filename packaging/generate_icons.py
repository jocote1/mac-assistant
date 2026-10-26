"""Rebuild the app mark and menu glyphs with Cocoa; no extra image dependencies."""
from pathlib import Path
import struct
import tempfile

from AppKit import (
    NSAffineTransform, NSBezierPath, NSBitmapImageRep, NSColor,
    NSGraphicsContext, NSDeviceRGBColorSpace, NSPNGFileType,
)


ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets"


def stroke(points, width=1.7):
    path = NSBezierPath.bezierPath()
    path.setLineWidth_(width)
    path.setLineCapStyle_(1)
    path.setLineJoinStyle_(1)
    path.moveToPoint_(points[0])
    for point in points[1:]:
        path.lineToPoint_(point)
    path.stroke()


def rect(x, y, width, height, radius=2, fill=False):
    path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(((x, y), (width, height)), radius, radius)
    path.setLineWidth_(1.6)
    path.fill() if fill else path.stroke()


def mark():
    # A voice waveform inside a speech bubble; shared by app and status bar.
    path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(((2, 4), (20, 17)), 5, 5)
    path.setLineWidth_(1.7)
    path.stroke()
    stroke([(8, 4), (5, 1.5), (5, 4)], 1.7)
    for x, low, high in [(7, 10, 15), (10.3, 8, 17), (13.7, 10, 15), (17, 9, 16)]:
        stroke([(x, low), (x, high)], 1.7)


def draw_glyph(name):
    if name == "idle":
        mark()
    elif name == "recording":
        rect(8.5, 9, 7, 12, 3.5)
        path = NSBezierPath.bezierPath()
        path.appendBezierPathWithArcWithCenter_radius_startAngle_endAngle_clockwise_((12, 12), 7, 180, 360, False)
        path.setLineWidth_(1.6)
        path.stroke()
        stroke([(12, 5), (12, 2)])
        stroke([(8, 2), (16, 2)])
    elif name == "busy":
        for x, y in [(4, 12), (12, 12), (20, 12)]:
            NSBezierPath.bezierPathWithOvalInRect_(((x - 1.8, y - 1.8), (3.6, 3.6))).fill()
    elif name == "stop":
        rect(5, 5, 14, 14, 3, True)
    elif name == "calendar":
        rect(3, 3, 18, 17)
        stroke([(3, 15), (21, 15)])
        for x in (8, 16):
            stroke([(x, 18), (x, 22)])
        for x in (8, 12, 16):
            rect(x - 1, 8, 2, 2, .5, True)
    elif name == "mail":
        rect(2, 5, 20, 15)
        stroke([(3, 19), (12, 11), (21, 19)])
    elif name == "moon":
        path = NSBezierPath.bezierPath()
        path.moveToPoint_((15, 22))
        path.curveToPoint_controlPoint1_controlPoint2_((21, 7), (6, 22), (8, 4))
        path.curveToPoint_controlPoint1_controlPoint2_((15, 22), (4, -2), (-1, 19))
        path.fill()
    elif name == "speaker":
        stroke([(3, 9), (7, 9), (12, 4), (12, 20), (7, 15), (3, 15), (3, 9)])
        for radius in (4, 8):
            path = NSBezierPath.bezierPath()
            path.appendBezierPathWithArcWithCenter_radius_startAngle_endAngle_clockwise_((12, 12), radius, -50, 50, False)
            path.setLineWidth_(1.6)
            path.stroke()
    elif name == "settings":
        for y, x in [(6, 8), (12, 16), (18, 10)]:
            stroke([(3, y), (21, y)])
            NSColor.whiteColor().setFill()
            rect(x - 2, y - 2.5, 4, 5, 1, True)
            NSColor.blackColor().setStroke()
            rect(x - 2, y - 2.5, 4, 5, 1)
    elif name == "logs":
        rect(4, 2, 16, 20)
        for y in (7, 12, 17):
            stroke([(8, y), (16, y)], 1.4)


def render(name, size, destination, app=False):
    bitmap = NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
        None, size, size, 8, 4, True, False, NSDeviceRGBColorSpace, 0, 0)
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.setCurrentContext_(NSGraphicsContext.graphicsContextWithBitmapImageRep_(bitmap))
    NSGraphicsContext.currentContext().setImageInterpolation_(3)
    transform = NSAffineTransform.transform()
    transform.scaleBy_(size / 24)
    transform.concat()
    if app:
        NSColor.colorWithCalibratedRed_green_blue_alpha_(.06, .10, .18, 1).setFill()
        rect(.5, .5, 23, 23, 5.3, True)
        NSColor.colorWithCalibratedRed_green_blue_alpha_(.40, .88, .82, 1).set()
        inset = NSAffineTransform.transform()
        inset.translateXBy_yBy_(4, 4)
        inset.scaleBy_(2 / 3)
        inset.concat()
        mark()
    else:
        NSColor.blackColor().set()
        draw_glyph(name)
    NSGraphicsContext.restoreGraphicsState()
    destination.write_bytes(bytes(bitmap.representationUsingType_properties_(NSPNGFileType, {})))


def main():
    ASSETS.mkdir(exist_ok=True)
    for name in ("idle", "recording", "busy", "stop", "calendar", "mail", "moon", "speaker", "settings", "logs"):
        render(name, 48, ASSETS / f"{name}.png")
    render("idle", 1024, ASSETS / "AppIcon.png", app=True)
    with tempfile.TemporaryDirectory() as folder:
        iconset = Path(folder) / "MacAssistant.iconset"
        iconset.mkdir()
        for size in (16, 32, 128, 256, 512):
            for scale in (1, 2):
                suffix = "@2x" if scale == 2 else ""
                render("idle", size * scale, iconset / f"icon_{size}x{size}{suffix}.png", app=True)
        # Modern ICNS stores PNG representations in length-prefixed chunks.
        # Writing the container directly also works without iconutil's system services.
        chunks = []
        for kind, filename in [
            (b"icp4", "icon_16x16.png"), (b"icp5", "icon_32x32.png"),
            (b"icp6", "icon_32x32@2x.png"), (b"ic07", "icon_128x128.png"),
            (b"ic08", "icon_256x256.png"), (b"ic09", "icon_512x512.png"),
            (b"ic10", "icon_512x512@2x.png"), (b"ic11", "icon_16x16@2x.png"),
            (b"ic12", "icon_32x32@2x.png"), (b"ic13", "icon_128x128@2x.png"),
            (b"ic14", "icon_256x256@2x.png"),
        ]:
            data = (iconset / filename).read_bytes()
            chunks.append(kind + struct.pack(">I", len(data) + 8) + data)
        payload = b"".join(chunks)
        (ASSETS / "AppIcon.icns").write_bytes(b"icns" + struct.pack(">I", len(payload) + 8) + payload)


if __name__ == "__main__":
    main()
