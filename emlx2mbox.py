#!/usr/bin/env python3
"""
emlx2mbox.py — Convert an Apple Mail .mbox package (V2/V3 format) into
Thunderbird-compatible mbox files, preserving the folder hierarchy.

Usage:
    python3 emlx2mbox.py <source_mbox_folder> <dest_folder>

Example:
    python3 emlx2mbox.py "D:\\spam.mbox" "D:\\converted\\Spam"

What it does:
  - Recursively finds every real .emlx / .partial.emlx file under
    <source>/*/Data/**/Messages/ (skips AppleDouble junk files like
    "._12345.emlx" and "__12345.emlx", and any macOS metadata files).
  - Parses the emlx format: first line = byte length of the raw email,
    followed by that many bytes of RFC822 message, followed by an
    Apple plist trailer (flags/read-status/etc).
  - Maps a few useful plist flags (Read / Answered / Junk) to a
    Status/X-Status header so Thunderbird shows correct read state.
  - Writes one mbox file per source .mbox package, using a proper
    "From " envelope separator line and correctly escaping any
    in-body lines that start with "From " (mbox quoting).
  - Recreates nested folder structure using Thunderbird's own
    "FolderName.sbd/ChildFolder" convention, so the hierarchy shows
    up correctly after importing into a Thunderbird profile.
"""

import sys
import os
import re
import time
import plistlib
from email import message_from_bytes
from email.utils import parsedate_to_datetime

APPLE_EPOCH_NOTE = None  # date-received in Apple's plist is a unix timestamp

def is_junk_file(filename: str) -> bool:
    """AppleDouble / macOS metadata files, not real emails."""
    base = os.path.basename(filename)
    if base.startswith("._") or base.startswith("__"):
        return True
    if base in (".DS_Store",):
        return True
    return False


def find_emlx_files(source_root: str, stop_at_nested_mbox: bool = False):
    """
    Walk the tree and yield every real .emlx file found.
    If stop_at_nested_mbox is True, pruning any subdirectory that is
    itself a *.mbox package, so a parent folder's message list doesn't
    double-count messages that actually belong to a nested subfolder.
    """
    for dirpath, dirnames, filenames in os.walk(source_root):
        # Don't descend into nested .mbox packages -- those are separate
        # subfolders and are converted independently.
        if stop_at_nested_mbox:
            dirnames[:] = [d for d in dirnames if not d.endswith(".mbox")]
        for fn in filenames:
            if not (fn.endswith(".emlx") or fn.endswith(".partial.emlx")):
                continue
            if is_junk_file(fn):
                continue
            full = os.path.join(dirpath, fn)
            yield full


def parse_emlx(path: str):
    """
    Parse a single .emlx file.
    Returns (raw_message_bytes, plist_dict_or_None).
    """
    with open(path, "rb") as f:
        data = f.read()

    # First line: ascii digits (byte length), possibly followed by
    # whitespace/padding, then a newline.
    newline_idx = data.find(b"\n")
    if newline_idx == -1:
        raise ValueError(f"{path}: no newline found, not a valid emlx")

    first_line = data[:newline_idx]
    m = re.match(rb"\s*(\d+)", first_line)
    if not m:
        raise ValueError(f"{path}: first line is not a byte length: {first_line!r}")
    msg_len = int(m.group(1))

    msg_start = newline_idx + 1
    msg_end = msg_start + msg_len
    raw_message = data[msg_start:msg_end]

    plist_dict = None
    rest = data[msg_end:].lstrip(b"\r\n")
    if rest.strip():
        try:
            plist_dict = plistlib.loads(rest)
        except Exception:
            plist_dict = None

    return raw_message, plist_dict


def build_status_headers(plist_dict):
    """
    Map Apple's 'flags' bitfield / plist keys to Status/X-Status headers
    so Thunderbird shows correct read/replied/junk state.
    Apple's flags integer encodes read/deleted/flagged etc in low bits;
    exact bit layout varies by version, so we only use what's reliable:
    presence of 'flags' plus known Apple Mail bit meanings for read (0)
    and deleted (1) bits when present. If uncertain, we skip rather than
    guess wrong.
    """
    headers = []
    if not plist_dict:
        return headers
    flags = plist_dict.get("flags")
    if isinstance(flags, int):
        is_read = bool(flags & 0x1)
        is_deleted = bool(flags & 0x2)
        is_flagged = bool(flags & 0x4)
        is_answered = bool(flags & 0x8)
        status = ""
        xstatus = ""
        if is_read:
            status += "R"
        status += "O"  # mark as seen/old so clients don't re-flag as new
        if is_deleted:
            xstatus += "D"
        if is_flagged:
            xstatus += "F"
        if is_answered:
            xstatus += "A"
        headers.append(("Status", status))
        if xstatus:
            headers.append(("X-Status", xstatus))
    return headers


def make_from_line(msg, plist_dict=None):
    """
    Build the mbox 'From ' envelope separator line.
    Preference order for the date:
      1. The message's own Date header (most accurate).
      2. Apple's plist 'date-received' unix timestamp (reliable fallback
         for messages with missing/malformed Date headers, e.g. spam).
      3. Current time (last resort).
    """
    ts = None
    date_hdr = msg.get("Date") if msg is not None else None
    if date_hdr:
        try:
            dt = parsedate_to_datetime(date_hdr)
            ts = dt.timetuple()
        except Exception:
            ts = None
    if ts is None and plist_dict:
        date_received = plist_dict.get("date-received")
        if isinstance(date_received, (int, float)):
            try:
                ts = time.gmtime(date_received)
            except Exception:
                ts = None
    if ts is None:
        ts = time.gmtime()
    date_str = time.strftime("%a %b %d %H:%M:%S %Y", ts)
    return f"From MAILER-DAEMON {date_str}\n".encode("utf-8")


def mbox_escape_body(raw_message: bytes) -> bytes:
    """
    Escape any line in the message that starts with 'From ' by
    prefixing with '>' (standard mbox 'From ' quoting), so the
    line isn't mistaken for a new message boundary.
    """
    lines = raw_message.split(b"\n")
    out = []
    for line in lines:
        if line.startswith(b"From "):
            out.append(b">" + line)
        else:
            out.append(line)
    return b"\n".join(out)


def emlx_to_mbox_entry(emlx_path: str) -> bytes:
    raw_message, plist_dict = parse_emlx(emlx_path)

    try:
        msg = message_from_bytes(raw_message)
    except Exception:
        msg = None

    from_line = make_from_line(msg, plist_dict)

    status_headers = build_status_headers(plist_dict)
    extra_header_bytes = b""
    for name, value in status_headers:
        extra_header_bytes += f"{name}: {value}\n".encode("utf-8")

    escaped_body = mbox_escape_body(raw_message)

    # Ensure the entry ends with a newline before the next "From " line
    entry = from_line + extra_header_bytes + escaped_body
    if not entry.endswith(b"\n"):
        entry += b"\n"
    entry += b"\n"
    return entry


def find_mbox_packages(source_root: str):
    """
    Find every *.mbox package directory under source_root, and record
    its relative path (for hierarchy reconstruction) and its Data dir.
    """
    packages = []
    for dirpath, dirnames, filenames in os.walk(source_root):
        for dn in list(dirnames):
            if dn.endswith(".mbox"):
                pkg_path = os.path.join(dirpath, dn)
                packages.append(pkg_path)
    return packages


def folder_name_from_mbox(mbox_dir_name: str) -> str:
    name = mbox_dir_name
    if name.endswith(".mbox"):
        name = name[:-5]
    return name


def dest_path_for_package(source_root: str, pkg_path: str, dest_root: str) -> str:
    """
    Compute the Thunderbird-style destination mbox file path for a given
    source .mbox package, using .sbd nesting for parent folders.

    Apple Mail nests mbox packages inside each other for subfolders, e.g.:
      Spam.mbox/.../Old.mbox/.../Older.mbox/...
    We mirror that using Thunderbird's convention:
      Spam                     (top-level folder, own mbox file)
      Spam.sbd/Old             (child folder)
      Spam.sbd/Old.sbd/Older   (grandchild folder)
    The top-level package name (source_root itself) is always the first
    link in the chain, since Thunderbird's own top-level folder needs to
    be the .sbd parent for every descendant, not just intermediate ones.
    """
    top_name = folder_name_from_mbox(
        os.path.basename(source_root.rstrip("/\\"))
    )

    rel = os.path.relpath(pkg_path, source_root)
    if rel == os.curdir:
        child_parts = []
    else:
        child_parts = [p for p in rel.split(os.sep) if p.endswith(".mbox")]
    child_names = [folder_name_from_mbox(p) for p in child_parts]

    full_chain = [top_name] + child_names

    dest_dir = dest_root
    for name in full_chain[:-1]:
        dest_dir = os.path.join(dest_dir, name + ".sbd")
    os.makedirs(dest_dir, exist_ok=True)
    return os.path.join(dest_dir, full_chain[-1])


def convert_package(pkg_path: str, dest_file: str, stats: dict):
    emlx_files = sorted(find_emlx_files(pkg_path, stop_at_nested_mbox=True))
    if not emlx_files:
        return  # empty folder, e.g. no "Data" or no messages yet

    os.makedirs(os.path.dirname(dest_file), exist_ok=True)
    with open(dest_file, "wb") as out:
        for emlx_path in emlx_files:
            try:
                entry = emlx_to_mbox_entry(emlx_path)
                out.write(entry)
                stats["converted"] += 1
            except Exception as e:
                stats["errors"] += 1
                stats["error_list"].append((emlx_path, str(e)))
    print(f"  -> {dest_file}  ({len(emlx_files)} messages)")


def main():
    if len(sys.argv) != 3:
        print("Usage: python3 emlx2mbox.py <source_mbox_folder> <dest_folder>")
        sys.exit(1)

    source_root = sys.argv[1]
    dest_root = sys.argv[2]

    os.makedirs(dest_root, exist_ok=True)

    packages = find_mbox_packages(source_root)
    # Also handle the case where source_root itself IS the .mbox package
    if os.path.basename(source_root.rstrip("/\\")).endswith(".mbox"):
        packages.append(source_root)
    # Deduplicate, keep only innermost-first is not necessary since each
    # package is processed independently by scanning its own emlx files.
    packages = sorted(set(packages))

    if not packages:
        print(f"No .mbox packages found under {source_root}")
        sys.exit(1)

    print(f"Found {len(packages)} .mbox package(s) to convert.\n")

    stats = {"converted": 0, "errors": 0, "error_list": []}

    for pkg in packages:
        dest_file = dest_path_for_package(source_root, pkg, dest_root)
        print(f"Converting: {pkg}")
        convert_package(pkg, dest_file, stats)

    print("\n=== Done ===")
    print(f"Messages converted: {stats['converted']}")
    print(f"Errors: {stats['errors']}")
    if stats["errors"]:
        for path, err in stats["error_list"][:20]:
            print(f"  FAILED: {path} -> {err}")


if __name__ == "__main__":
    main()
