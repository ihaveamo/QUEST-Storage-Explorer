#!/usr/bin/env python3
"""Quest 3 App Explorer: read-only ADB inventory -> offline interactive HTML.
AUTHOR: MICHAEL FORDE
VERSION: 2.2
DATE: 23/11/26


Usage: py -3 -m pip install androguard
       py -3 quest3_app_explorer.py
       py -3 quest3_app_explorer.py --depth 5
       py -3 quest3_app_explorer.py --all
       py -3 quest3_app_explorer.py --no-large-files

Requires Python 3.10+ and Android platform-tools (adb) on PATH.
For automatic first-run setup on Windows, use Start-Quest3-Explorer.cmd.
"""



import argparse
import io
import os
import struct
import tempfile
import zipfile
import zlib
from datetime import datetime
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import webbrowser

# Androguard logs via Loguru, which otherwise prints blue DEBUG diagnostics.
# INFO / WARNING / ERROR remain visible. Set LOGURU_LEVEL=DEBUG beforehand
# only when diagnosing APK parsing problems.
os.environ.setdefault('LOGURU_LEVEL', 'INFO')

OUTPUT = Path(__file__).resolve().parent / 'Quest3-App-Explorer.html'
LABEL_CACHE = Path(__file__).resolve().parent / 'Quest3-App-Labels.json'


def adb(*args, timeout=60, check=True):
    result = subprocess.run(['adb', *args], capture_output=True,
                            text=True, encoding='utf-8', errors='replace',
                            timeout=timeout)
    if check and result.returncode:
        raise RuntimeError((result.stderr or result.stdout).strip() or 'ADB command failed')
    return result.stdout.replace('\r', '')


def line_value(source, key):
    match = re.search(r'(?m)^\s*' + re.escape(key) + r'=([^\r\n]*)', source)
    return match.group(1).strip() if match else ''


def section_for_package(source, pkg):
    start = re.search(r'(?m)^\s*Package \[' + re.escape(pkg) + r'\]\s*\([^\n]+\):', source)
    if start:
        sub = source[start.end():]
        end = re.search(r'(?m)^\s{2}Package \[|^\S[^\n]*:\s*$', sub)
        return sub[:end.start()] if end else sub
    return source


def requested_permissions(source):
    # dumpsys formatting varies; only extract clearly indented permission names.
    lines = source.splitlines()
    for index, line in enumerate(lines):
        if line.strip() == 'requested permissions:':
            perms = []
            for entry in lines[index + 1:]:
                if not entry.strip():
                    continue
                if re.match(r'^\s{0,4}\S', entry):
                    break
                item = entry.strip().split(':', 1)[0].strip()
                if re.match(r'^[a-zA-Z_][\w.]+$', item):
                    perms.append(item)
            return perms[:300]
    return []


def packages_with_meta(include_system):
    args = [] if include_system else ['-3']
    paths = {}
    for line in adb('shell', 'pm', 'list', 'packages', *args, '-f').splitlines():
        if line.startswith('package:'):
            path, divider, pkg = line[len('package:'):].rpartition('=')
            if divider and pkg:
                paths[pkg.strip()] = path.strip()

    installer = {}
    for line in adb('shell', 'pm', 'list', 'packages', *args, '-i').splitlines():
        m = re.match(r'package:(\S+)(?:\s+installer=(\S+))?', line)
        if m:
            installer[m.group(1)] = m.group(2) or ''

    uids = {}
    for line in adb('shell', 'pm', 'list', 'packages', *args, '-U').splitlines():
        m = re.match(r'package:(\S+)(?:\s+uid:(\d+))?', line)
        if m:
            uids[m.group(1)] = m.group(2) or ''

    # If -f is unsupported on a vendor image, retain package names from -i.
    names = sorted(set(paths) | set(installer) | set(uids))
    return names, paths, installer, uids


def diskstats():
    raw = adb('shell', 'dumpsys', 'diskstats', timeout=75)
    labels = ('Package Names', 'App Sizes', 'App Data Sizes', 'Cache Sizes')
    fields = {}
    for label in labels:
        m = re.search(r'(?m)^\s*' + re.escape(label) + r':\s*(\[[^\n]*\])', raw)
        if not m:
            return {}, f'Android did not expose the {label} array in dumpsys diskstats.'
        try:
            fields[label] = json.loads(m.group(1))
        except ValueError:
            return {}, f'Android returned invalid JSON for {label}.'

    if len({len(a) for a in fields.values()}) != 1:
        return {}, 'Storage arrays have unequal lengths; sizes were intentionally omitted.'

    sizes = {}
    for pkg, code, data, cache in zip(*(fields[k] for k in labels)):
        if isinstance(pkg, str) and all(isinstance(x, int) and x >= 0 for x in (code, data, cache)):
            sizes[pkg] = {'app': code, 'data': data, 'cache': cache, 'total': code + data}
    return sizes, ''


def shared_folder_usage():
    # This may fail on recent Android builds because of /Android/data permissions.
    # Size here can OVERLAP Android's app data estimate: never add it to totals.
    command = 'du -k -s /sdcard/Android/data/* /sdcard/Android/obb/* 2>/dev/null'
    raw = adb('shell', command, timeout=180, check=False)
    entries = {}
    for line in raw.splitlines():
        m = re.match(r'^\s*(\d+)\s+(/(?:sdcard|storage/emulated/0)/Android/(data|obb)/([^/\r\n]+))\s*$', line)
        if m:
            size = int(m.group(1)) * 1024
            category, name = m.group(3), m.group(4)
            entries.setdefault(name, {})[category] = size
    return entries


# Labels are APK resource strings, not a field in pm list packages.
# Read only ZIP members needed for AndroidManifest/resources, without pulling games.
def adb_bytes(*args, timeout=55):
    r = subprocess.run(['adb', 'exec-out', *args], stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE, timeout=timeout)
    if r.returncode:
        raise RuntimeError(r.stderr.decode('utf-8', 'replace').strip()[:150])
    return r.stdout


def apk_member_fast(apk_path, member):
    # Works on devices where unzip is installed. Binary exec-out avoids CRLF corruption.
    b = adb_bytes('unzip', '-p', apk_path, member, timeout=50)
    if not b:
        raise ValueError('Empty ZIP member')
    return b


def device_apk_size(apk_path):
    out = adb('shell', 'stat', '-c', '%s', apk_path, timeout=20).strip()
    return int(out.splitlines()[-1])


def device_read_range(apk_path, offset, length):
    if offset < 0 or length < 0 or length > 128 * 1024 * 1024:
        raise ValueError('Invalid ZIP range')
    block = 4096
    start = (offset // block) * block
    blocks = (offset - start + length + block - 1) // block
    if not blocks:
        return b''
    raw = adb_bytes('dd', f'if={apk_path}', f'bs={block}',
                    f'skip={start // block}', f'count={blocks}', timeout=80)
    data = raw[offset - start: offset - start + length]
    if len(data) != length:
        raise ValueError('Truncated APK data transfer')
    return data


def device_zip_members(apk_path, wanted=('AndroidManifest.xml', 'resources.arsc')):
    """Read selected ZIP members over adb, even when the Quest lacks unzip.

    A normal APK is a ZIP, with its index at the end. Fetch its index and two
    resource members by byte ranges, avoiding transfer of large game assets.
    """
    size = device_apk_size(apk_path)
    tail_len = min(size, 131072)
    tail = adb_bytes('tail', '-c', str(tail_len), apk_path, timeout=45)
    e = tail.rfind(b'PK\x05\x06')
    if e < 0 or e + 22 > len(tail):
        raise ValueError('ZIP end-of-directory not found')
    sig, disk, cd_disk, n_disk, n_total, cd_size, cd_off, comment_len = struct.unpack_from('<IHHHHIIH', tail, e)
    if (disk or cd_disk or cd_off == 0xFFFFFFFF or cd_size == 0xFFFFFFFF
            or cd_size > 64 * 1024 * 1024):
        raise ValueError('Unsupported ZIP layout')
    cd = device_read_range(apk_path, cd_off, cd_size)
    entries = {}
    pos = 0
    while pos + 46 <= len(cd):
        values = struct.unpack_from('<IHHHHHHIIIHHHHHII', cd, pos)
        if values[0] != 0x02014b50:
            break
        method, compressed_len, uncompressed_len = values[4], values[8], values[9]
        name_len, extra_len, comment_len = values[10], values[11], values[12]
        local_off = values[16]
        name = cd[pos+46:pos+46+name_len].decode('utf-8', 'replace')
        if name in wanted:
            entries[name] = (local_off, method, compressed_len, uncompressed_len)
        pos += 46 + name_len + extra_len + comment_len
    if 'AndroidManifest.xml' not in entries:
        raise ValueError('No APK manifest found')
    out = {}
    for name, (local_off, method, csize, usize) in entries.items():
        if csize > 128 * 1024 * 1024:
            continue
        hdr = device_read_range(apk_path, local_off, 30)
        if hdr[:4] != b'PK\x03\x04':
            continue
        nlen, xlen = struct.unpack_from('<HH', hdr, 26)
        payload = device_read_range(apk_path, local_off + 30 + nlen + xlen, csize)
        if method == 8:
            payload = zlib.decompress(payload, -15)
        elif method != 0:
            continue
        if len(payload) != usize:
            raise ValueError('APK member length mismatch')
        out[name] = payload
    return out



def optional_call(app, method, default=None):
    """Isolate vendor APK oddities and Androguard API differences."""
    try:
        target = getattr(app, method, None)
        return target() if callable(target) else default
    except Exception:
        return default


def parse_apk_metadata(app):
    """Manifest-only metadata; does not decode/decompile DEX or extract game assets."""
    def strings(method):
        values = optional_call(app, method, []) or []
        return sorted(set(str(x) for x in values if x))[:160]
    activities = strings('get_activities')
    return {
        'mainActivity': str(optional_call(app, 'get_main_activity', '') or ''),
        'features': strings('get_features'),
        'nativeLibraries': strings('get_libraries'),
        'manifestPermissions': strings('get_permissions'),
        'activitiesCount': len(activities),
        'servicesCount': len(strings('get_services')),
        'receiversCount': len(strings('get_receivers')),
        'providersCount': len(strings('get_providers')),
    }


def read_device_app_metadata(apk_path, APK, *, pull_max_mb=0):
    """Return label, metadata, diagnostic. Fetch only APK manifest/resources where possible."""
    if not apk_path:
        return '', {}, 'No APK path reported by Android.'
    failures = []
    members = None
    try:
        members = {name: apk_member_fast(apk_path, name)
                   for name in ('AndroidManifest.xml', 'resources.arsc')}
    except (RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
        failures.append('Device unzip: ' + str(exc)[:110])
        try:
            members = device_zip_members(apk_path)
        except (RuntimeError, ValueError, OSError, struct.error,
                zlib.error, subprocess.TimeoutExpired) as exc2:
            failures.append('ZIP range extractor: ' + str(exc2)[:150])
    if members is not None and 'AndroidManifest.xml' in members:
        try:
            stream = io.BytesIO()
            with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_STORED) as archive:
                for name, content in members.items():
                    archive.writestr(name, content)
            instance = APK(stream.getvalue(), raw=True)
            label = (instance.get_app_name() or '').strip()
            extra = parse_apk_metadata(instance)
            if label or extra.get('mainActivity') or extra.get('features'):
                return label, extra, ''
            failures.append('APK parsed but label and extra metadata were missing')
        except Exception as exc:
            failures.append(f'Androguard: {type(exc).__name__}: {str(exc)[:160]}')
    if pull_max_mb > 0:
        try:
            size = device_apk_size(apk_path)
            if size <= int(pull_max_mb * 1048576):
                with tempfile.TemporaryDirectory(prefix='quest-apk-') as folder:
                    dest = str(Path(folder) / 'base.apk')
                    res = subprocess.run(['adb', 'pull', apk_path, dest], capture_output=True,
                                         text=True, encoding='utf-8', errors='replace', timeout=240)
                    if res.returncode:
                        failures.append('ADB pull: ' + (res.stderr or res.stdout).strip()[:120])
                    else:
                        obj = APK(dest)
                        return (obj.get_app_name() or '').strip(), parse_apk_metadata(obj), ''
            else:
                failures.append(f'Full APK fallback skipped: {size/1048576:.0f} MiB')
        except Exception as exc:
            failures.append(f'Full APK fallback: {str(exc)[:130]}')
    return '', {}, '; '.join(failures) or 'No APK metadata available'


def read_label_cache():
    if LABEL_CACHE.exists():
        try:
            val = json.loads(LABEL_CACHE.read_text(encoding='utf-8'))
            return val if isinstance(val, dict) else {}
        except (OSError, ValueError):
            pass
    return {}


def obtain_names(apps, enabled=True, *, single_package=False):
    if not enabled:
        return 'APK metadata extraction disabled.'
    try:
        from androguard.core.apk import APK
        # Androguard also exposes its own Loguru configuration helper.
        # Apply INFO explicitly in case the installed version overrides
        # the default Loguru sink established by LOGURU_LEVEL above.
        try:
            from androguard.util import set_log
            set_log(os.environ.get('LOGURU_LEVEL', 'INFO').upper())
        except Exception:
            pass  # Logging configuration should never prevent scanning.
    except (ImportError, ModuleNotFoundError):
        msg = 'Androguard unavailable. Install using py -3 -m pip install androguard'
        print('WARNING:', msg)
        for a in apps: a['labelError'] = msg
        return msg
    except Exception as exc:
        msg = f'Androguard could not initialise: {type(exc).__name__}: {exc}'
        print('WARNING:', msg)
        return msg

    cache = read_label_cache()
    successes = 0
    metadata_count = 0
    for i, app in enumerate(apps, 1):
        key = f"{app['package']}|{app['versionCode']}|{app['apkPath']}"
        previous = cache.get(key)
        if isinstance(previous, dict) and previous.get('name'):
            app['name'] = previous['name']
            app['apkMetadata'] = previous.get('apkMetadata', {})
        else:
            print(f'  APK metadata {i}/{len(apps)}: {app["package"]}', flush=True)
            try:
                # Existing string-only cache entries will be refreshed to collect
                # the additional manifest metadata once, then reused thereafter.
                label, extra, error = read_device_app_metadata(
                    app['apkPath'], APK, pull_max_mb=(512 if single_package else 0))
                app['name'] = label or (previous if isinstance(previous, str) else '')
                app['apkMetadata'] = extra
                app['labelError'] = error
                if app['name']:
                    cache[key] = {'name': app['name'], 'apkMetadata': extra}
            except Exception as exc:
                app['labelError'] = f'{type(exc).__name__}: {str(exc)[:140]}'
        if app['name']: successes += 1
        if app['apkMetadata']: metadata_count += 1
        if single_package:
            print('     Name:', app['name'] or 'Unavailable')
            print('     Main activity:', app['apkMetadata'].get('mainActivity') or 'Unavailable')
            if app['labelError']:
                print('     Diagnostic:', app['labelError'])
    try:
        LABEL_CACHE.write_text(json.dumps(cache, indent=2, ensure_ascii=False), encoding='utf-8')
    except OSError as exc:
        print('Warning writing label cache:', exc)
    msg = f'{successes}/{len(apps)} display names, {metadata_count} APK metadata records.'
    print('APK status:', msg)
    return msg


# STORAGE AUDIT. All observations are read-only. They are separate perspectives,
# NOT additive totals: Android stats frequently overlap with shared storage du.

def read_partition():
    """Parse live df -k on /data. /sdcard is an emulated view of this same storage."""
    errors = []
    for path in ('/data', '/storage/emulated/0'):
        try:
            output = adb('shell', 'df', '-k', path, timeout=25, check=False)
            lines = output.splitlines()
            for line in reversed(lines):
                parts = line.split()
                if len(parts) < 6 or not all(x.isdigit() for x in parts[1:4]):
                    continue
                total, used, free = (int(x)*1024 for x in parts[1:4])
                return {'total':total,'used':used,'free':free,
                        'filesystem':parts[0],'mount':parts[-1], 'path':path}, ''
            errors.append(output[:170].strip() or f'No parseable df output for {path}')
        except Exception as exc:
            errors.append(str(exc)[:140])
    return {}, '; '.join(errors)


def read_android_categories():
    """Android's cached diskstats media/other/system estimates, independent of df."""
    names = {
        'photos': 'Photos Size', 'videos': 'Videos Size',
        'audio': 'Audio Size', 'downloads': 'Downloads Size',
        'system': 'System Size', 'other': 'Other Size',
        'appCodeAggregate': 'App Size',
        'appDataAggregate': 'App Data Size',
        'appCacheAggregate': 'Cache Size',
    }
    try:
        raw = adb('shell', 'dumpsys', 'diskstats', timeout=75, check=False)
    except Exception as exc:
        return {}, str(exc)
    found = {}
    for key, label in names.items():
        m = re.search(r'(?mi)^\s*' + re.escape(label) + r':\s*([0-9]+)\b', raw)
        if m:
            found[key] = int(m.group(1))
    return found, ('' if found else 'Android did not expose aggregate category sizes.')


def parse_du_line(line):
    m = re.match(r'^\s*(\d+)\s+(/\S.*?|/sdcard)\s*$', line)
    if not m: return None
    path = m.group(2).rstrip('/') or '/'
    if path.startswith('/storage/emulated/0'):
        path = '/sdcard' + path[len('/storage/emulated/0'):]
    return {'path': path, 'bytes': int(m.group(1))*1024}


def shell_quote(s):
    return "'" + str(s).replace("'", "'\\''") + "'"


def scan_folder_tree(*, max_depth=3):
    """Measured /sdcard directory tree, independent of package storage stats.

    Toybox `du -d` is supported on many Android builds; use per-folder fallback
    when unavailable. Keep errors visible: denied folders are not zero-sized.
    """
    warnings = []
    try:
        result = subprocess.run(['adb', 'shell', 'du', '-k', '-d', str(max_depth), '/sdcard'],
                                capture_output=True, text=True, encoding='utf-8',
                                errors='replace', timeout=420)
        rows = [parse_du_line(line) for line in result.stdout.replace('\r','').splitlines()]
        rows = [r for r in rows if r]
        if result.stderr.strip():
            warnings.append('du access warning: ' + result.stderr.strip()[:280])
        if len(rows) > 1 and (result.returncode == 0 or rows):
            return rows, warnings, 'recursive'
        warnings.append('Recursive du depth option not supported or no folder data; using fallback.')
    except subprocess.TimeoutExpired:
        warnings.append('Recursive folder scan timed out; using shallow fallback.')
    except Exception as exc:
        warnings.append(f'Recursive folder scan failed: {str(exc)[:130]}')
    # Fallback: first and second level, exact shell quoting via ls + du.
    observed = []
    try:
        listing = adb('shell', 'ls', '-1', '-A', '/sdcard', timeout=25, check=False)
        candidates = ['/sdcard/' + line for line in listing.splitlines()
                      if line.strip() not in ('.','..') and not line.startswith('ls:')
                      and '/' not in line and '\x00' not in line]
        for path in candidates[:250]:
            out = adb('shell', 'du -k -s ' + shell_quote(path) + ' 2>/dev/null',
                      timeout=90, check=False)
            for line in out.splitlines():
                row = parse_du_line(line)
                if row: observed.append(row)
        out = adb('shell', 'du -k -s /sdcard 2>/dev/null', timeout=300, check=False)
        for line in out.splitlines():
            row = parse_du_line(line)
            if row: observed.append(row)
    except Exception as exc:
        warnings.append('Shallow folder scan failed: ' + str(exc)[:170])

    # On some Quest firmware the recursive `du -d` option isn't available.
    # The ordinary fallback lists only top-level folders, hiding the contents
    # of Android (typically the largest folder). Measure its key children in
    # one extra pass so data/obb/media and per-package storage can be inspected.
    if max_depth >= 2 and not any(r['path'].startswith('/sdcard/Android/') for r in observed):
        try:
            paths = ['/sdcard/Android/data', '/sdcard/Android/obb', '/sdcard/Android/media']
            # Whole-branch totals, followed by direct package children.
            cmd = 'du -k -s ' + ' '.join(paths + [p + '/*' for p in paths]) + ' 2>/dev/null'
            output = adb('shell', cmd, timeout=420, check=False)
            for line in output.splitlines():
                row = parse_du_line(line)
                if row and row['path'] not in {x['path'] for x in observed}:
                    observed.append(row)
        except subprocess.TimeoutExpired:
            warnings.append('Android data/obb folder drill-down timed out; top-level sizes remain available.')
        except Exception as exc:
            warnings.append('Android data/obb drill-down failed: ' + str(exc)[:170])
    return observed, warnings, 'shallow + Android drill-down'


def scan_large_files(min_mb=128, top_count=120):
    """Read-only discovery of large shared-storage files; no device-side writes."""
    # Android Toybox find -size uses 512-byte blocks (unlike GNU find's M).
    # The explicit block value works on Quest's Android shell.
    threshold_blocks = min_mb * 1048576 // 512
    command = ("find /sdcard -type f -size +%d "
               "-exec stat -c '%%s|%%n' '{}' ';'" % threshold_blocks)
    try:
        result = subprocess.run(['adb', 'shell', command], capture_output=True,
                                text=True, encoding='utf-8', errors='replace', timeout=420)
        output = result.stdout.replace('\r', '')
        warning = ''
        if result.returncode:
            warning = 'Large-file scan incomplete (permissions or find/stat failure): ' + result.stderr.strip()[:250]
    except subprocess.TimeoutExpired:
        return [], 'Large-file scan timed out, so results are unavailable.'
    except Exception as exc:
        return [], 'Large-file scan failed: ' + str(exc)[:160]
    records = []
    for line in output.splitlines():
        first, sep, path = line.partition('|')
        if sep and first.isdecimal() and path.startswith('/sdcard/'):
            records.append({'path':path, 'bytes':int(first)})
    records.sort(key=lambda a: a['bytes'], reverse=True)
    return records[:top_count], warning


def shared_folder_usage_from_tree(tree):
    data = {}
    pat = re.compile(r'^/sdcard/Android/(data|obb|media)/([^/]+)$')
    for item in tree:
        m = pat.match(item['path'])
        if m:
            data.setdefault(m.group(2), {})[m.group(1)] = item['bytes']
    return data




def scan_android_ownership(folders, installed_packages, apps):
    """Inventory direct children of Android/obb, data and media; match package IDs.

    Reuse already-measured du entries and specifically measure only missing items.
    All operations are read-only. An unmatched name is a REVIEW candidate, never
    proof of removable material. Permissions can cause lower-bound measurements.
    """
    existing = {f['path']: f['bytes'] for f in folders if isinstance(f.get('bytes'), int)}
    labels = {a['package']: a.get('name') or '' for a in apps}
    warnings = []
    grouped = {}
    for kind in ('obb', 'data', 'media'):
        parent = '/sdcard/Android/' + kind
        # Listing adds entries skipped by a partial/permission-denied du scan.
        listing = adb('shell', 'ls', '-1', '-A', parent, timeout=35, check=False)
        names = [ln.strip() for ln in listing.splitlines()
                 if ln.strip() not in ('.', '..') and not ln.startswith('ls:')
                 and '/' not in ln and '\x00' not in ln]
        # If ls itself was denied, we can still use previously measured paths.
        if not names and ('Permission denied' in listing or 'No such file' in listing):
            warnings.append(f'Could not list {parent}; ownership scan may be incomplete.')
        for path in existing:
            if path.startswith(parent + '/') and '/' not in path[len(parent)+1:]:
                name = path[len(parent)+1:]
                if name not in names:
                    names.append(name)
        names = sorted(set(names))
        missing_paths = [parent + '/' + name for name in names
                         if parent + '/' + name not in existing]
        for start in range(0, len(missing_paths), 35):
            paths = missing_paths[start:start+35]
            command = 'du -k -s ' + ' '.join(shell_quote(path) for path in paths)
            try:
                res = subprocess.run(['adb', 'shell', command], capture_output=True,
                                     text=True, encoding='utf-8', errors='replace',
                                     timeout=240)
                for ln in res.stdout.replace('\r', '').splitlines():
                    entry = parse_du_line(ln)
                    if entry:
                        existing[entry['path']] = entry['bytes']
                if 'Permission denied' in res.stderr:
                    warnings.append(f'Some {kind} entries could not be fully measured (permissions).')
                elif res.returncode and not res.stdout.strip():
                    warnings.append(f'Size scan failed for some {kind} entries: {res.stderr.strip()[:160]}')
            except subprocess.TimeoutExpired:
                warnings.append(f'Timed out measuring some {kind} entries; unmeasured sizes shown as unknown.')
            except Exception as exc:
                warnings.append(f'Error measuring {kind}: {str(exc)[:170]}')
        for name in names:
            path = parent + '/' + name
            row = grouped.setdefault(name, {
                'package': name,
                'installed': name in installed_packages,
                'name': labels.get(name, ''),
                'obb': None, 'data': None, 'media': None,
                'paths': {}, 'unmeasured': []
            })
            row[kind] = existing.get(path)
            row['paths'][kind] = path
            if row[kind] is None:
                row['unmeasured'].append(kind)
    rows = list(grouped.values())
    for row in rows:
        values = [row[k] for k in ('obb', 'data', 'media') if isinstance(row[k], int)]
        row['measuredTotal'] = sum(values) if values else None
    rows.sort(key=lambda r: (r['measuredTotal'] if r['measuredTotal'] is not None else -1),
              reverse=True)
    return rows, sorted(set(warnings))


def build_findings(all_names, apps, folders, files, partition, categories, warnings):
    """Actionable clues, carefully avoiding claims that an unknown is safely deletable."""
    findings = []
    def add(priority, title, detail, path='', amount=None, action='Review before deleting'):
        findings.append({'priority':priority, 'title':title,'detail':detail,
                         'path':path,'bytes':amount,'action':action})
    root_names = {r['path']:r for r in folders}
    for path, heading in [('/sdcard/Oculus/VideoShots','Quest video recordings'),
                          ('/sdcard/Oculus/Screenshots','Quest screenshots'),
                          ('/sdcard/Movies','Movies'),('/sdcard/DCIM','Camera media'),
                          ('/sdcard/Download','Downloads'),('/sdcard/Downloads','Downloads')]:
        row = root_names.get(path)
        if row and row['bytes'] >= 200 * 1048576:
            add('review', heading, 'Likely personal media/downloads: back up any wanted files before removing duplicates or old recordings.',
                path, row['bytes'], 'Inspect with Quest Files or adb pull; delete selected files manually')
    for folder in folders:
        m = re.match(r'^/sdcard/Android/(data|obb|media)/([^/]+)$', folder['path'])
        if m and all_names and folder['bytes'] >= 100 * 1048576 and m.group(2) not in all_names:
            add('investigate', 'Possibly leftover app folder',
                'No currently installed package has this exact ID. This may be an old app, shared assets or a retained save. Confirm before removing.',
                folder['path'], folder['bytes'], 'Verify app ownership; back up first')
    for f in files[:100]:
        if f['bytes'] >= 1024**3:
            add('review', 'Large shared-storage file',
                'Check whether this is a recording, installer, ISO, ZIP, downloaded game file or required runtime asset.',
                f['path'], f['bytes'], 'Inspect the filename and purpose')
    missing_count = len([a for a in apps if a['total'] is None])
    if missing_count:
        add('measurement', 'Some app storage sizes unavailable',
            f'{missing_count} inventoried package(s) have no diskstats size. Device usage is therefore not fully represented by per-app totals.',
            action='Check the storage report and firmware reporting')
    if not folders:
        add('measurement', 'Shared-storage scan unavailable',
            'ADB could not measure /sdcard folders. This prevents identifying large video and download directories.',
            action='Check access warnings in Diagnostics')
    if not categories:
        add('measurement', 'Android category sizes unavailable',
            'No system media/category breakdown was returned by dumpsys diskstats.',
            action='Review file-tree and device partition views instead')
    if not partition:
        add('measurement', 'Physical partition usage unavailable',
            'The report cannot currently reconcile used/free space against app storage.',
            action='Check adb shell df -k /data')
    if not findings:
        add('info', 'No obvious large removable files identified',
            'This does not mean the headset has no reclaimable space: protected private app data and system storage may remain inaccessible.',
            action='Review biggest apps and folder tree')
    findings.sort(key=lambda f: (0 if f['priority']=='investigate' else 1 if f['priority']=='review' else 2,
                                 -(f['bytes'] or 0)))
    return findings[:100]


def generate_html(apps, context, audit):
    payload = json.dumps({'apps':apps,'context':context,'audit':audit}, ensure_ascii=False,
                         separators=(',',':'))
    payload = payload.replace('<','\\u003c').replace('>','\\u003e').replace('&','\\u0026')
    return r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Quest 3 · Storage Explorer</title>
<style>
:root{color-scheme:dark;--bg:#09111d;--panel:#122036;--panel2:#1b3049;--edge:#30455f;--text:#e7f1ff;--muted:#a7b9cf;--teal:#5dddd0;--blue:#6599f9;--gold:#efc481;--pink:#e38caf;--purple:#bb9af7}
*{box-sizing:border-box}body{background:var(--bg);color:var(--text);font:14px/1.5 system-ui,Segoe UI,Arial,sans-serif;margin:0}main{max-width:1500px;margin:auto;padding:30px 23px 65px}h1{font-size:clamp(26px,3.5vw,43px);letter-spacing:-.05em;margin:0 0 3px}h2{font-size:19px;letter-spacing:-.02em;margin:0}h3{font-size:15px;margin:0 0 9px}p{margin:7px 0 12px}.muted{color:var(--muted)}.small{font-size:12px}.lead{color:var(--muted);max-width:850px;margin-bottom:16px}.eyebrow{text-transform:uppercase;letter-spacing:.14em;font-size:11px;color:var(--teal);font-weight:800}
.pills{display:flex;flex-wrap:wrap;gap:8px;margin:13px 0 24px}.pill{background:#19283b;border:1px solid var(--edge);border-radius:20px;padding:5px 11px;color:#c1d2e7;font-size:12px}
.cards{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:11px;margin-bottom:12px}.card{background:linear-gradient(145deg,#1d344c,#122034);border:1px solid var(--edge);border-radius:14px;padding:16px}.card .cap{color:var(--muted);font-size:12px}.card .num{font-size:clamp(21px,2.3vw,31px);letter-spacing:-.04em;font-weight:800;margin:4px 0}.card .sub{font-size:12px;color:#b4c5d6}
section{margin-top:18px;background:var(--panel);border:1px solid var(--edge);border-radius:16px;overflow:hidden}.sectiontop{display:flex;flex-wrap:wrap;gap:12px;justify-content:space-between;align-items:end;padding:18px 19px 9px}.sectionbody{padding:13px 19px 20px}.hint{font-size:12px;color:var(--muted);margin:6px 0 0}.warning{color:var(--gold)}.empty{color:var(--muted);padding:14px 0}.mark{border-radius:6px;background:#263d54;padding:2px 5px}.legend{display:flex;gap:15px;flex-wrap:wrap;font-size:12px;color:var(--muted);margin:10px 0}.dot{display:inline-block;width:10px;height:10px;margin-right:5px;border-radius:2px}.horizontal{display:flex;height:21px;background:#22364a;border-radius:8px;overflow:hidden}.horizontal>span{display:block;min-width:0}.barrow{display:grid;grid-template-columns:minmax(160px,2.2fr) minmax(110px,4fr) 95px;gap:14px;align-items:center;margin:9px 0}.barname{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.bartrack{height:16px;background:#24364b;border-radius:6px;overflow:hidden;display:flex}.barpiece{height:100%;min-width:0}.barval{font-variant-numeric:tabular-nums;text-align:right;white-space:nowrap;color:#d6e5f7}.bars{padding-bottom:6px}.caption{color:var(--muted);font-size:12px;padding-top:7px}.cols{display:grid;grid-template-columns:1fr 1fr;gap:18px}.controls{display:flex;align-items:center;gap:9px;flex-wrap:wrap}input,select{background:#0d1c30;border:1px solid #3d5571;border-radius:9px;color:var(--text);font:inherit;padding:9px 12px;min-height:39px}input{min-width:265px;width:min(32vw,390px)}button{font:inherit;cursor:pointer}.mini{font-size:12px;border:1px solid var(--edge);background:#233752;color:var(--text);padding:6px 9px;border-radius:7px}.mini:hover{background:#315272}
.tablewrap{overflow:auto}table{border-collapse:collapse;width:100%;min-width:840px}th,td{padding:10px 12px;text-align:left;vertical-align:top;border-bottom:1px solid #243b52}th{color:#c7d6e9;font-size:12px;background:#1b3049;white-space:nowrap}th button{padding:0;background:transparent;border:0;color:inherit;font-weight:700}tr.data:hover{background:#1b3046}td.right,th.right{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}.primary{font-weight:700}.secondary{font-size:12px;color:var(--muted);overflow-wrap:anywhere}.path{font-family:Consolas,monospace;font-size:12px;overflow-wrap:anywhere}.namebtn{border:0;background:transparent;color:var(--text);padding:0;font:inherit;font-weight:700;text-align:left}.namebtn:hover{color:var(--teal)}.detail td{background:#091a2a;padding:17px}.detailgrid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px}.field{border:1px solid var(--edge);padding:10px;border-radius:9px;min-width:0}.field b{display:block;color:var(--muted);font-size:12px;margin-bottom:4px}.field span{font-size:13px;overflow-wrap:anywhere;white-space:pre-wrap}.wide{grid-column:1/-1}.code{font-family:Consolas,monospace}.status{font-size:11px;text-transform:uppercase;letter-spacing:.08em;color:var(--gold);font-weight:800}.finding{border-top:1px solid #2c4057;padding:12px 19px}.finding:first-child{border-top:0}.finding b{font-size:14px}.finding p{color:var(--muted);font-size:12px;margin:3px 0}.finding .amount{color:var(--teal);font-weight:750;float:right}.notice{border-left:3px solid var(--gold);background:#1b2b3e;padding:12px 15px;margin-top:11px;color:#d8e4f2;font-size:13px}.faq{padding:13px 19px;border-top:1px solid var(--edge)}.faq summary{font-weight:700;cursor:pointer}.faq p{color:var(--muted)}.scroll{max-height:680px;overflow:auto}.details-row td{padding:0}.folders .namebtn{font-family:Consolas,monospace;font-weight:500;font-size:13px}.folders .namebtn:hover{text-decoration:underline}.meter{height:26px;display:flex;overflow:hidden;border-radius:8px;background:#253d57}.meter span{height:100%}.topnav{display:flex;flex-wrap:wrap;gap:8px;margin-top:15px}.topnav a{color:#c7e7ff;text-decoration:none;background:#1a2e44;border:1px solid var(--edge);border-radius:8px;padding:6px 10px;font-size:12px}.topnav a:hover{background:#284969}.sectiontop .small{margin-top:4px}
@media(max-width:900px){.cards{grid-template-columns:repeat(2,minmax(0,1fr))}.cols{grid-template-columns:1fr}.detailgrid{grid-template-columns:1fr 1fr}.barrow{grid-template-columns:minmax(115px,1.5fr) minmax(90px,2fr) 80px;gap:8px}.sectiontop{padding:15px}main{padding:18px 10px}}
@media(max-width:540px){.cards{grid-template-columns:1fr 1fr}.detailgrid{grid-template-columns:1fr}input{width:100%;min-width:0}.controls{width:100%}.barrow{grid-template-columns:110px minmax(50px,1fr) 70px}.barname{font-size:12px}}
</style></head><body><main>
<div class="eyebrow">ADB · Offline HTML · Read-only</div><h1>Quest 3 Storage Explorer</h1>
<p class="lead">Where did your storage go? Compare the headset's real occupied space, Android's app accounting and the contents of shared storage. Expand individual games and folders to find practical cleanup candidates.</p>
<div id="meta" class="pills"></div>
<nav class="topnav"><a href="#overview">Overview</a><a href="#breakdown">Storage categories</a><a href="#topapps">Largest games</a><a href="#folders">Folders</a><a href="#ownership">OBB / app data owners</a><a href="#files">Large files</a><a href="#findings">Cleanup clues</a><a href="#inventory">All apps</a><a href="#diagnostics">Diagnostics</a></nav>
<section id="overview"><div class="sectiontop"><div><h2>1. Actual device capacity</h2><div class="hint">Live <code>df -k /data</code> figures, not the sum of individual apps.</div></div></div>
<div class="sectionbody"><div class="cards"><div class="card"><div class="cap">Partition capacity</div><div class="num" id="capacity">—</div><div class="sub">Usable data filesystem, not advertised flash size</div></div><div class="card"><div class="cap">Used on partition</div><div class="num" id="used">—</div><div class="sub">Includes apps, private files and filesystem overhead</div></div><div class="card"><div class="cap">Free on partition</div><div class="num" id="free">—</div><div class="sub">Space reported as currently available</div></div><div class="card"><div class="cap">Visible shared storage</div><div class="num" id="sharedTotal">—</div><div class="sub" id="sharedSummaryLabel">Measured /sdcard tree; overlaps with app data</div></div></div><div class="meter" id="capacityMeter"></div><div class="legend" id="capacityLegend"></div><div class="notice">Do not subtract the app total from <code>df</code> and assume the difference is junk. System-reserved space, private app data, multiple users, snapshots, metadata, and cached estimates can prevent an exact reconciliation.</div></div></section>
<section id="breakdown"><div class="sectiontop"><div><h2>2. Android's categorized storage</h2><div class="hint">Cached <code>dumpsys diskstats</code> categories: independently measured; not guaranteed to match live <code>df</code>.</div></div></div><div class="sectionbody"><div id="categoryBars" class="bars"></div><div class="notice" id="categoryWarning">Cache is already included in app data. The category totals are approximate and must not be added to the folder sizes below.</div></div></section>
<div class="cols"><section id="topapps"><div class="sectiontop"><div><h2>3. Biggest installed games</h2><div class="hint">APK/code + app data; highlighted cache is part of data, not extra.</div></div><select id="appCount"><option value="15">Top 15</option><option value="30">Top 30</option><option value="100">Top 100</option></select></div><div class="sectionbody"><div class="legend"><span><i class="dot" style="background:var(--blue)"></i>App/code</span><span><i class="dot" style="background:var(--purple)"></i>Other data</span><span><i class="dot" style="background:var(--pink)"></i>Cache within data</span></div><div id="appBars" class="bars"></div></div></section>
<section id="folderbars"><div class="sectiontop"><div><h2>4. Largest top-level folders</h2><div class="hint">Physical /sdcard folder sizes; do not add to app totals.</div></div></div><div class="sectionbody"><div id="folderBars" class="bars"></div><div class="notice"><b>/sdcard/Android is NOT the Android operating system.</b> It commonly holds installed games' large OBB expansion packages, app-specific data and media. Inspect its <code>obb</code>, <code>data</code> and <code>media</code> children before considering deletion. Other folders may contain sideloaded ISOs, VR-port assets, recordings or downloads. Some protected directories may be unmeasured.</div></div></section></div>
<section id="folders"><div class="sectiontop"><div><h2>5. Explore directories</h2><p class="small muted">Expand arrows to inspect nested folders and identify the largest space users. Parent sizes <b>include</b> their children.</p></div><input id="folderSearch" type="search" placeholder="Find folders, app IDs, Oculus, downloads…"></div><div class="tablewrap scroll"><table class="folders"><thead><tr><th>Path / folder</th><th class="right">Size</th><th class="right" id="folderPercentHeader">Percentage of shared storage</th></tr></thead><tbody id="folderRows"></tbody></table></div><div class="hint" id="folderPercentNote" style="padding:12px 19px">Only a limited directory depth is scanned by default; a folder can contain deeper files. Access-denied directories may be absent rather than 0 bytes.</div></section>

<section id="ownership"><div class="sectiontop"><div><h2>6. Who owns Android/obb and Android/data?</h2><div class="hint">Matches each immediate folder name against <code>pm list packages</code> (including system apps). Unmatched means <b>no exact installed package ID</b>, not necessarily disposable.</div></div><div class="controls"><input type="search" id="ownerSearch" placeholder="Search game name or folder ID…"><select id="ownerFilter"><option value="all">All entries</option><option value="installed">Installed apps</option><option value="unmatched">Not matched to installed package</option><option value="unknown">Unmeasured folders</option></select></div></div>
<div class="sectionbody"><div class="cards"><div class="card"><div class="cap">Matched folders</div><div class="num" id="ownerMatched">—</div><div class="sub">Distinct package names with installed matches</div></div><div class="card"><div class="cap">Unmatched folders</div><div class="num" id="ownerUnmatched">—</div><div class="sub">Review these before deleting anything</div></div><div class="card"><div class="cap">Measured unmatched storage</div><div class="num" id="ownerUnmatchedBytes">—</div><div class="sub">Lower bound where measurements were denied</div></div><div class="card"><div class="cap">Unmeasured entries</div><div class="num" id="ownerUnknown">—</div><div class="sub">Access limitations; size is not zero</div></div></div><div class="notice">Folder sizes can <b>overlap with Android's per-app data statistics</b>: never add them to the app totals. A folder not matching an installed package could belong to an uninstalled game, a different user/profile, a manually copied installation, or shared assets. Back up important data and verify ownership before any removal.</div></div>
<div class="tablewrap scroll"><table style="min-width:1100px"><thead><tr><th>Game / folder / package ID</th><th>Package match</th><th class="right">OBB</th><th class="right">Data</th><th class="right">Media</th><th class="right">Measured combined</th></tr></thead><tbody id="ownerRows"></tbody></table></div><div class="hint" id="ownerShown" style="padding:12px 19px"></div></section>
<section id="files"><div class="sectiontop"><div><h2>7. Large individual files</h2><div class="hint">Read-only scan of large files in /sdcard. Useful for finding recordings, ZIPs, ISOs, cached downloads and old installers.</div></div><input id="fileSearch" type="search" placeholder="Filter filenames…"></div><div class="tablewrap scroll"><table><thead><tr><th>Filename / full path</th><th class="right">Size</th><th>Likely kind</th></tr></thead><tbody id="fileRows"></tbody></table></div></section>
<section id="findings"><div class="sectiontop"><div><h2>8. Cleanup clues</h2><div class="hint">Suggestions based on measured paths, never automatic deletions.</div></div></div><div id="findingRows"></div><div class="notice" style="margin:12px 19px 18px">Before deleting: check whether files are game assets or personal saves, back up anything important, and avoid removing <code>/data</code>, Meta system packages, or folders you cannot identify. This tool deliberately includes no delete/uninstall buttons.</div></section>
<section id="inventory"><div class="sectiontop"><div><h2>9. All installed applications</h2><div class="hint" id="appShown">Click a name for locations, dates, permissions, components and Androguard information.</div></div><div class="controls"><input id="appSearch" type="search" placeholder="Search name, package, installer, permissions…"><select id="appFilter"><option value="all">All</option><option value="known">Known storage</option><option value="unknown">Unknown storage</option><option value="cache">Cache over 100 MiB</option></select></div></div><div class="tablewrap scroll"><table style="min-width:1180px"><thead><tr><th><button data-sort="name">Display name ↕</button></th><th><button data-sort="package">Package ↕</button></th><th class="right"><button data-sort="total">Total ↕</button></th><th class="right"><button data-sort="app">APK/code ↕</button></th><th class="right"><button data-sort="data">Data ↕</button></th><th class="right"><button data-sort="cache">Cache ↕</button></th><th><button data-sort="installed">Installed ↕</button></th><th><button data-sort="updated">Updated ↕</button></th><th><button data-sort="version">Version ↕</button></th></tr></thead><tbody id="appRows"></tbody></table></div></section>
<section id="diagnostics"><div class="sectiontop"><div><h2>10. Diagnostics and interpretation</h2><div class="hint">Collection warnings and methodology; useful if numbers appear wrong.</div></div></div><div id="warnings" class="sectionbody"></div><details class="faq"><summary>Which totals can I add together?</summary><p>You may add app code + app data for one package. App cache is inside the data total: do not add it again. The shared-storage filesystem is often counted by Android as app data too, so /sdcard folder sizes must not be added to per-app figures. /sdcard and /data are generally views of the same underlying user-data volume, not independent devices.</p></details><details class="faq"><summary>Why is the device using more space than all my games?</summary><p>Android and Meta system services, private app storage, MediaStore media, downloads, system updates, logs, caches, protected paths and filesystem allocation all contribute. Some are restricted to the unprivileged ADB shell and cannot be fully attributed. Android's per-app/category accounting can also be stale.</p></details><details class="faq"><summary>What can Androguard tell us?</summary><p>APK manifest data: application display name, launch activity, requested permissions, hardware features, native shared libraries, component counts, and (where Android supplies them) SDK versions. It does not identify every user-facing store title, inspect external asset files, determine whether data is safe to remove, or measure storage. No DEX decompilation is performed.</p></details><details class="faq"><summary>Can I remove the files listed here?</summary><p>Not automatically. For games, use the Quest library or adb uninstall only after deciding to remove them. For large recordings, downloads and old ZIPs, inspect and back up before removing individual files in Quest Files. Android/data, Android/obb, Unknown Sources, Meta services and shared Quest ports may contain data needed by other applications.</p></details></section>
<p class="small muted" style="margin-top:20px">This is a local offline HTML snapshot. Refresh it by running the Python script again. No files are uploaded or removed by the script.</p>
</main><script>
const S=__INVENTORY_JSON__, apps=S.apps||[], ctx=S.context||{}, a=S.audit||{};
const id=n=>document.getElementById(n),esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmt=n=>!Number.isFinite(n)?'Not available':(n>=1073741824?(n/1073741824).toFixed(2)+' GiB':n>=1048576?(n/1048576).toFixed(1)+' MiB':n>=1024?(n/1024).toFixed(0)+' KiB':n+' B');
const pct=(n,d)=>Number.isFinite(n)&&Number.isFinite(d)&&d>0?(100*n/d).toFixed(1)+'%':'—';
const known=x=>Number.isFinite(x.total);
const colours={blue:'#6599f9',purple:'#bb9af7',pink:'#e38caf',teal:'#5dddd0',gold:'#efc481'};
const fld=(label,value,wide=false)=>`<div class="field ${wide?'wide':''}"><b>${esc(label)}</b><span>${esc(value??'Not reported')}</span></div>`;
const list=x=>Array.isArray(x)&&x.length?x.join('\n'):'Not reported';
function initMeta(){id('meta').innerHTML=[ctx.model,ctx.android,ctx.generated,ctx.scope,ctx.labelStatus,a.folderScanMode].filter(Boolean).map(v=>`<span class="pill">${esc(v)}</span>`).join('');}
function initCapacity(){const p=a.partition||{};id('capacity').textContent=fmt(p.total);id('used').textContent=fmt(p.used);id('free').textContent=fmt(p.free);id('sharedTotal').textContent=folderAccounting.total>0?fmt(folderAccounting.total):'Unknown';id('sharedSummaryLabel').textContent=folderAccounting.approximate?'Sum of measured top-level folders (minimum, not exact /sdcard total)':'Measured /sdcard tree; overlaps with app data';if(Number.isFinite(p.total)){const u=Math.max(0,Math.min(100,100*p.used/p.total)),free=Math.max(0,Math.min(100,100*p.free/p.total)),unavailable=Math.max(0,100-u-free);id('capacityMeter').innerHTML=`<span style="background:var(--blue);width:${u}%"></span><span style="background:var(--teal);width:${free}%"></span><span style="background:var(--gold);width:${unavailable}%"></span>`;id('capacityLegend').innerHTML=`<span><i class="dot" style="background:var(--blue)"></i>Used ${fmt(p.used)} (${u.toFixed(1)}%)</span><span><i class="dot" style="background:var(--teal)"></i>Available ${fmt(p.free)}</span>${unavailable>0.05?`<span><i class="dot" style="background:var(--gold)"></i>Reserved / unavailable ${fmt(Math.max(0,p.total-p.used-p.free))}</span>`:''}<span>${esc(p.mount||p.path||'')}</span>`}else id('capacityMeter').textContent='Device partition size unavailable';}
function renderBars(target,items,maxItems=25){const slice=items.filter(x=>x.bytes>0).slice(0,maxItems);if(!slice.length){id(target).innerHTML='<p class="empty">No sizes available.</p>';return;}const max=Math.max(...slice.map(x=>x.bytes));id(target).innerHTML=slice.map(x=>`<div class="barrow"><span class="barname" title="${esc(x.full||x.name)}">${esc(x.name)}</span><div class="bartrack"><span class="barpiece" style="background:${esc(x.colour||colours.teal)};width:${(100*x.bytes/max).toFixed(3)}%"></span></div><span class="barval">${fmt(x.bytes)}</span></div>`).join('');}
function renderCats(){const cs=a.categories||{}, appSizes=a.allAppSizes||{};let items=[];if(Number.isFinite(cs.appCodeAggregate)||Number.isFinite(appSizes.code)){items.push({name:'App code / APKs',bytes:cs.appCodeAggregate??appSizes.code,colour:colours.blue});items.push({name:'App data (includes cache)',bytes:cs.appDataAggregate??appSizes.data??0,colour:colours.purple})}for(const [key,label,col] of [['videos','Videos',colours.pink],['photos','Photos',colours.teal],['audio','Audio',colours.gold],['downloads','Downloads',colours.blue],['other','Other / unclassified',colours.purple],['system','System estimate',colours.gold]])if(Number.isFinite(cs[key]))items.push({name:label,bytes:cs[key],colour:col});items.sort((x,y)=>y.bytes-x.bytes);renderBars('categoryBars',items,30);const p=a.partition||{};let total=items.reduce((s,x)=>s+x.bytes,0);const message=Number.isFinite(p.used)?`Category items shown total ${fmt(total)}, versus live partition usage ${fmt(p.used)}. A difference of ${fmt(Math.abs(p.used-total))} is not automatically removable storage; category definitions, update times and partitions can differ.`:'Category totals cannot be reconciled without live partition data.';id('categoryWarning').textContent=message+' Cache (a subset of app data): '+fmt(cs.appCacheAggregate??appSizes.cache)+'; not shown as an extra category.';}
function renderAppBars(){const rows=apps.filter(known).sort((x,y)=>y.total-x.total).slice(0,Number(id('appCount').value));if(!rows.length){id('appBars').innerHTML='<p class="empty">No per-app sizes available.</p>';return;}const max=rows[0].total;id('appBars').innerHTML=rows.map(x=>{const cache=Math.max(0,Math.min(x.data||0,x.cache||0));const normalData=Math.max(0,(x.data||0)-cache);const parts=[[x.app,colours.blue],[normalData,colours.purple],[cache,colours.pink]].filter(z=>z[0]>0);return `<div class="barrow"><span class="barname" title="${esc(x.package)}">${esc(x.name||x.package)}</span><div class="bartrack" title="Code ${fmt(x.app)} · Data ${fmt(x.data)} · Cache ${fmt(x.cache)}">${parts.map(([s,c])=>`<span class="barpiece" style="background:${c};width:${(100*s/max).toFixed(3)}%"></span>`).join('')}</div><span class="barval">${fmt(x.total)}</span></div>`}).join('');}
const folders=a.folders||[],folderMap=new Map(folders.map(x=>[x.path,x]));
const par=path=>path.slice(0,path.lastIndexOf('/'))||'/';
const topLevel=folders.filter(f=>par(f.path)==='/sdcard'&&Number.isFinite(f.bytes)&&f.bytes>=0);
const topLevelSum=topLevel.reduce((total,f)=>total+f.bytes,0);
const rootSize=folderMap.get('/sdcard')?.bytes;
const rootValid=Number.isFinite(rootSize)&&rootSize>0&&rootSize>=topLevelSum;
const folderAccounting={total:rootValid?rootSize:(topLevelSum>0?topLevelSum:null),approximate:!rootValid};

function renderFolderBars(){const items=folders.filter(x=>par(x.path)==='/sdcard').map(x=>({name:x.path.slice(8),full:x.path,bytes:x.bytes,colour:colours.teal})).sort((x,y)=>y.bytes-x.bytes);renderBars('folderBars',items,22);}
const appDisplayNames=new Map(apps.map(app=>[app.package,app.name||'']));
const expandedFolders=new Set(['/sdcard','/sdcard/Android']);
function renderFolderTree(){const q=id('folderSearch').value.toLowerCase().trim();let rows=[];let fs=[];if(q){fs=folders.filter(x=>x.path.toLowerCase().includes(q)).sort((x,y)=>y.bytes-x.bytes).slice(0,300);}else {function descend(parent,depth){const kids=folders.filter(x=>par(x.path)===parent).sort((x,y)=>y.bytes-x.bytes);for(const f of kids){fs.push({...f,depth});if(expandedFolders.has(f.path)&&depth<8)descend(f.path,depth+1)}}descend('/sdcard',0)}const denom=folderAccounting.total;id('folderPercentHeader').textContent=folderAccounting.approximate?'Share of measured top-level folders':'Percentage of shared storage';id('folderPercentNote').textContent=folderAccounting.approximate?'The /sdcard root total was missing or inconsistent. Percentages use the sum of measured top-level folders as an approximate denominator. These are NOT percentages of total Quest capacity. Hidden or inaccessible files may be missing.':'Percentages refer to the measured /sdcard root total, NOT total Quest capacity. Parent folder sizes include children; access-denied items may be unmeasured.';rows=fs.map(f=>{const depth=f.depth||0;const kids=folders.some(x=>par(x.path)===f.path);const inner=q?f.path:('　'.repeat(depth)+(kids?(expandedFolders.has(f.path)?'▾ ':'▸ '):'• ')+f.path.split('/').at(-1));const pkg=(f.path.match(/^\/sdcard\/Android\/(?:data|obb|media)\/([^/]+)$/)||[])[1];const human=pkg?appDisplayNames.get(pkg):'';return `<tr class="data"><td><button class="namebtn" title="${esc(f.path)}" data-folder="${esc(f.path)}">${esc(inner)}</button>${human?` <span class="secondary">— ${esc(human)}</span>`:''}${q?'':`<div class="secondary path" style="padding-left:${depth*13+19}px">${esc(f.path)}</div>`}</td><td class="right">${fmt(f.bytes)}</td><td class="right">${pct(f.bytes,denom)}</td></tr>`}).join('');id('folderRows').innerHTML=rows||'<tr><td colspan="3" class="empty">No accessible subfolders at this depth or matching this filter.</td></tr>';}
function kind(path){const name=path.toLowerCase();if(/\.(mp4|mkv|avi|mov|webm)$/.test(name))return 'Video recording / movie';if(/\.(iso|chd|cso|bin|img)$/.test(name))return 'Disc / game image';if(/\.(zip|7z|rar|tar|gz)$/.test(name))return 'Archive / download';if(/\.(apk|apks|xapk)$/.test(name))return 'Android installer';if(/\.(obb|pak|bundle|unity3d)$/.test(name))return 'Game assets / OBB';if(/\.(jpg|png|webp)$/.test(name))return 'Image';return 'Other / unknown';}

function renderOwnership(){
  const data=a.ownership||[];
  const unmatched=data.filter(x=>!x.installed);
  const unknown=data.filter(x=>(x.unmeasured||[]).length);
  id('ownerMatched').textContent=data.filter(x=>x.installed).length;
  id('ownerUnmatched').textContent=unmatched.length;
  const amount=unmatched.reduce((sum,x)=>sum+(Number.isFinite(x.measuredTotal)?x.measuredTotal:0),0);
  id('ownerUnmatchedBytes').textContent=fmt(amount);
  id('ownerUnknown').textContent=unknown.length;
  const q=id('ownerSearch').value.toLowerCase().trim(), filter=id('ownerFilter').value;
  const selected=data.filter(x=>{
    if(filter==='installed'&&!x.installed)return false;
    if(filter==='unmatched'&&x.installed)return false;
    if(filter==='unknown'&&!(x.unmeasured||[]).length)return false;
    return !q||[x.package,x.name,...Object.values(x.paths||{})].some(v=>String(v||'').toLowerCase().includes(q));
  });
  const cell=(x,k)=>Number.isFinite(x[k])?fmt(x[k]):((x.paths||{})[k]?'Not measurable':'—');
  id('ownerRows').innerHTML=selected.length?selected.map(x=>`<tr class="data">
    <td><div class="primary">${esc(x.name||x.package)}</div><div class="secondary path">${esc(x.package)}</div>${!x.installed?`<div class="secondary">Possible leftover or shared folder — verify first</div>`:''}</td>
    <td><span class="status" style="color:${x.installed?'var(--teal)':'var(--gold)'}">${x.installed?'Installed':'No package match'}</span></td>
    <td class="right" title="${esc((x.paths||{}).obb||'')}">${cell(x,'obb')}</td>
    <td class="right" title="${esc((x.paths||{}).data||'')}">${cell(x,'data')}</td>
    <td class="right" title="${esc((x.paths||{}).media||'')}">${cell(x,'media')}</td>
    <td class="right primary">${fmt(x.measuredTotal)}</td>
  </tr>`).join(''):'<tr><td colspan="6" class="empty">No matching entries; check the scan diagnostics.</td></tr>';
  id('ownerShown').textContent=`${selected.length} of ${data.length} distinct folders · sorted by measured size. Unknown sizes do not mean zero bytes.`;
}

function renderFiles(){const term=id('fileSearch').value.toLowerCase().trim();const rows=(a.largeFiles||[]).filter(x=>!term||x.path.toLowerCase().includes(term));id('fileRows').innerHTML=rows.length?rows.map(f=>`<tr class="data"><td><div class="primary">${esc(f.path.split('/').at(-1))}</div><div class="path muted">${esc(f.path)}</div></td><td class="right">${fmt(f.bytes)}</td><td>${esc(kind(f.path))}</td></tr>`).join(''):'<tr><td colspan="3" class="empty">No large files found (or the scan was unavailable). Check Diagnostics.</td></tr>';}
function renderFindings(){const rows=a.findings||[];id('findingRows').innerHTML=rows.map(f=>`<div class="finding"><span class="amount">${Number.isFinite(f.bytes)?fmt(f.bytes):''}</span><span class="status">${esc(f.priority)}</span> <b>${esc(f.title)}</b><p>${esc(f.detail)}</p>${f.path?`<div class="path muted">${esc(f.path)}</div>`:''}<div class="secondary">Suggested: ${esc(f.action)}</div></div>`).join('')||'<p class="empty">No findings.</p>';}
let appSort='total',descending=true;const openedApps=new Set();
function appDetails(x){const m=x.apkMetadata||{};let parts=[fld('Name',x.name),fld('Package',x.package),fld('Version',x.version),fld('Version code',x.versionCode),fld('Install date',x.installed),fld('Last updated',x.updated),fld('Installer',x.installer),fld('UID',x.uid),fld('Target SDK',x.targetSdk),fld('Min SDK',x.minSdk),fld('CPU ABI',x.abi),fld('App/code size',fmt(x.app)),fld('App data (includes cache)',fmt(x.data)),fld('Cache (subset of data)',fmt(x.cache)),fld('Android/data',fmt(x.sharedData)),fld('Android/obb',fmt(x.sharedObb)),fld('Android/media',fmt(x.sharedMedia)),fld('Android launch activity',m.mainActivity),fld('Activities declared',m.activitiesCount),fld('Services declared',m.servicesCount),fld('Broadcast receivers',m.receiversCount),fld('Content providers',m.providersCount),fld('Installed APK',x.apkPath,true),fld('Code directory',x.codePath,true),fld('Private data directory',x.dataPath,true),fld('Manifest hardware features',list(m.features),true),fld('Manifest native libraries',list(m.nativeLibraries),true),fld('APK permissions (requested)',list(m.manifestPermissions),true),fld('Package manager requested permissions',list(x.permissions),true)];if(x.error)parts.push(fld('Data retrieval error',x.error,true));if(x.labelError)parts.push(fld('APK parsing diagnostic',x.labelError,true));return `<tr class="detail"><td colspan="9"><div class="detailgrid">${parts.join('')}</div></td></tr>`;}
function renderApps(){const term=id('appSearch').value.trim().toLowerCase(),filter=id('appFilter').value;const rows=apps.filter(x=>{if(filter==='known'&&!known(x))return false;if(filter==='unknown'&&known(x))return false;if(filter==='cache'&&!(x.cache>100*1048576))return false;return !term||[x.name,x.package,x.installer,x.version,x.apkPath,...(x.permissions||[])].some(v=>String(v||'').toLowerCase().includes(term))});rows.sort((x,y)=>{const v=x[appSort],w=y[appSort],na=v===null||v===undefined||v==='',nb=w===null||w===undefined||w==='';if(na!==nb)return na?1:-1;const out=typeof v==='number'&&typeof w==='number'?v-w:String(v||'').localeCompare(String(w||''),undefined,{numeric:true,sensitivity:'base'});return descending?-out:out});id('appShown').textContent=`${rows.length} of ${apps.length} apps · click headings to sort, names for details`;id('appRows').innerHTML=rows.length?rows.map(x=>`<tr class="data"><td><button class="namebtn" data-app="${esc(x.package)}">${openedApps.has(x.package)?'▾':'▸'} ${esc(x.name||'Name unavailable')}</button></td><td class="secondary">${esc(x.package)}</td><td class="right" style="color:var(--teal)">${fmt(x.total)}</td><td class="right">${fmt(x.app)}</td><td class="right">${fmt(x.data)}</td><td class="right">${fmt(x.cache)}</td><td>${esc(x.installed||'—')}</td><td>${esc(x.updated||'—')}</td><td>${esc(x.version||'—')}</td></tr>${openedApps.has(x.package)?appDetails(x):''}`).join(''):'<tr><td colspan="9" class="empty">No matching apps.</td></tr>';}
function renderDiagnostics(){const warnings=[...(a.warnings||[])];if(ctx.storageWarning)warnings.push(ctx.storageWarning);if(a.partitionWarning)warnings.push('Partition: '+a.partitionWarning);if(a.categoryWarning)warnings.push('Categories: '+a.categoryWarning);if(a.fileWarning)warnings.push('Large files: '+a.fileWarning);warnings.push('Shared folder measurement: '+(a.folderScanMode||'disabled'));warnings.push('Inventory: '+(ctx.scope||'unknown')+'; app totals do not necessarily include system packages.');warnings.push('Large-file threshold: '+(a.fileThreshold||'disabled'));id('warnings').innerHTML=warnings.map(w=>`<p class="small ${w.includes('Warning')||w.includes('failed')||w.includes('unavailable')?'warning':'muted'}">• ${esc(w)}</p>`).join('');}
id('appCount').addEventListener('change',renderAppBars);id('folderSearch').addEventListener('input',renderFolderTree);id('fileSearch').addEventListener('input',renderFiles);id('ownerSearch').addEventListener('input',renderOwnership);id('ownerFilter').addEventListener('change',renderOwnership);id('appSearch').addEventListener('input',renderApps);id('appFilter').addEventListener('change',renderApps);
id('folderRows').addEventListener('click',ev=>{let btn=ev.target.closest('[data-folder]');if(!btn)return;const path=btn.getAttribute('data-folder');if(expandedFolders.has(path))expandedFolders.delete(path);else expandedFolders.add(path);renderFolderTree()});id('appRows').addEventListener('click',ev=>{let btn=ev.target.closest('[data-app]');if(!btn)return;const key=btn.getAttribute('data-app');openedApps.has(key)?openedApps.delete(key):openedApps.add(key);renderApps()});document.querySelectorAll('button[data-sort]').forEach(btn=>btn.addEventListener('click',()=>{const key=btn.dataset.sort;if(key===appSort)descending=!descending;else{appSort=key;descending=['total','app','data','cache'].includes(key)}renderApps()}));
initMeta();initCapacity();renderCats();renderAppBars();renderFolderBars();renderFolderTree();renderOwnership();renderFiles();renderFindings();renderApps();renderDiagnostics();
</script></body></html>'''.replace('__INVENTORY_JSON__',payload)


def main():
    parser=argparse.ArgumentParser(description='Quest 3 storage explorer: full offline HTML audit')
    parser.add_argument('--all',action='store_true',help='Include system packages in app table (slower)')
    parser.add_argument('--only',metavar='PACKAGE',help='Inspect one package (still scans shared storage)')
    parser.add_argument('--no-shared',action='store_true',help='Skip /sdcard directory scan')
    parser.add_argument('--no-large-files',action='store_true',help='Skip large-file search')
    parser.add_argument('--large-file-mb',type=int,default=128,help='Large-file minimum size in MiB, default 128')
    parser.add_argument('--depth',type=int,default=4,help='Shared-storage directory depth, default 4 (1-6)')
    parser.add_argument('--no-labels',action='store_true',help='Skip Androguard APK metadata')
    parser.add_argument('--no-open',action='store_true',help='Write HTML but do not open browser')
    args=parser.parse_args()
    if not (1<=args.depth<=6): parser.error('--depth must be between 1 and 6')
    if not (16<=args.large_file_mb<=4096): parser.error('--large-file-mb must be 16 to 4096')
    if not shutil.which('adb'):
        sys.exit('Cannot find adb.exe on PATH. Test adb devices first.')
    try:
        devices=adb('devices')
        connected=[line.split()[0] for line in devices.splitlines()[1:]
                   if len(line.split())>=2 and line.split()[1]=='device']
        if len(connected)!=1:
            sys.exit(f'Connect exactly one authorised Quest. Found {len(connected)}.\n{devices}')
        print('Connected:',connected[0],flush=True)
        model=adb('shell','getprop','ro.product.model').strip()
        android=adb('shell','getprop','ro.build.version.release').strip()
        print('[1/6] Reading partition and Android cached storage categories...',flush=True)
        partition,partition_warning=read_partition()
        categories,category_warning=read_android_categories()
        sizes,storage_warning=diskstats()
        print('[2/6] Reading installed package list...',flush=True)
        names,paths,installers,uids=packages_with_meta(args.all)
        if args.only:
            if args.only not in names:
                sys.exit(f'Package not found: {args.only}')
            names=[args.only]
        if not names:
            sys.exit('No apps detected. Check adb shell pm list packages -3')
        all_packages={line.split('package:',1)[1].strip() for line in
                      adb('shell','pm','list','packages').splitlines() if line.startswith('package:')}
        apps=[]
        print('[3/6] Reading per-app package metadata...',flush=True)
        for i,pkg in enumerate(names,1):
            print(f'  {i}/{len(names)}: {pkg}',flush=True)
            try:
                output=section_for_package(adb('shell','dumpsys','package',pkg,timeout=45),pkg)
                error=''
            except Exception as exc:
                output='';error=str(exc)
            code=line_value(output,'versionCode')
            stats=sizes.get(pkg,{})
            # pm list packages -f already provides paths; only use pm path if missing.
            path=paths.get(pkg,'')
            if not path:
                raw=adb('shell','pm','path',pkg,check=False)
                path=next((x.removeprefix('package:').strip() for x in raw.splitlines()
                           if x.startswith('package:')),'')
            apps.append({'package':pkg,'name':'','labelError':'','apkMetadata':{},
                'version':line_value(output,'versionName'),
                'versionCode':code.split()[0] if code else '',
                'installed':line_value(output,'firstInstallTime'),
                'updated':line_value(output,'lastUpdateTime'),
                'apkPath':path,'codePath':line_value(output,'codePath'),
                'dataPath':line_value(output,'dataDir'),
                'installer':(installers.get(pkg) if installers.get(pkg) not in ('null','None','') else '') or line_value(output,'installerPackageName'),
                'uid':uids.get(pkg) or line_value(output,'userId'),
                'targetSdk':(re.search(r'\btargetSdk=(\d+)',output) or [None,''])[1],
                'minSdk':(re.search(r'\bminSdk=(\d+)',output) or [None,''])[1],
                'abi':line_value(output,'primaryCpuAbi'),
                'permissions':requested_permissions(output),
                'app':stats.get('app'),'data':stats.get('data'),'cache':stats.get('cache'),
                'total':stats.get('total'),'sharedData':None,'sharedObb':None,
                'sharedMedia':None,'error':error})
        print('[4/6] Scanning shared storage folders...',flush=True)
        folders=[];warnings=[];folder_mode='disabled';file_warning='';large_files=[]
        if not args.no_shared:
            folders,warnings,folder_mode=scan_folder_tree(max_depth=args.depth)
            tops=[x['bytes'] for x in folders if x['path'].count('/')==2 and x['path'].startswith('/sdcard/')]
            root=next((x['bytes'] for x in folders if x['path']=='/sdcard'), None)
            if not isinstance(root,int) or root<=0 or root<sum(tops):
                warnings.append('The /sdcard root size is missing or smaller than measured child folders. Folder percentages use a measured top-level subtotal, not total headset capacity.')
            print(f'  Found {len(folders)} measured folders. Scan type: {folder_mode}',flush=True)
            usage=shared_folder_usage_from_tree(folders)
            for app in apps:
                entry=usage.get(app['package'],{})
                app['sharedData']=entry.get('data')
                app['sharedObb']=entry.get('obb')
                app['sharedMedia']=entry.get('media')
            if not args.no_large_files:
                print('[5/6] Finding large files (read-only)...',flush=True)
                large_files,file_warning=scan_large_files(min_mb=args.large_file_mb)
                print(f'  Found {len(large_files)} large files (showing at most 120).',flush=True)
        print('[6/6] Reading APK labels + additional Androguard manifest metadata...',flush=True)
        label_status=obtain_names(apps,enabled=not args.no_labels,single_package=bool(args.only))
        print('      Matching Android/obb, data and media folders to installed packages...', flush=True)
        ownership,owner_warnings=(scan_android_ownership(folders,all_packages,apps)
                                  if not args.no_shared else ([],[]))
        warnings.extend(owner_warnings)
        print(f'      Ownership: {len(ownership)} folder IDs, {sum(not x["installed"] for x in ownership)} unmatched.', flush=True)
        apps.sort(key=lambda x:x['total'] if x['total'] is not None else -1,reverse=True)
        context={'model':model or 'Quest','android':'Android '+android,
                 'generated':'Generated '+datetime.now().strftime('%Y-%m-%d %H:%M'),
                 'scope':('Package '+args.only) if args.only else ('All installed packages' if args.all else 'Third-party packages'),
                 'storageWarning':storage_warning,'labelStatus':label_status}
        # Summaries over all packages; do not mistake third-party totals for full accounting.
        all_app_sizes={'code':sum(x['app'] for x in sizes.values()),
                       'data':sum(x['data'] for x in sizes.values()),
                       'cache':sum(x['cache'] for x in sizes.values())} if sizes else {}
        findings=build_findings(all_packages,apps,folders,large_files,partition,categories,warnings)
        audit={'partition':partition,'partitionWarning':partition_warning,
               'categories':categories,'categoryWarning':category_warning,
               'allAppSizes':all_app_sizes,'folders':folders,'ownership':ownership,
               'folderScanMode':folder_mode,'largeFiles':large_files,
               'fileThreshold':f'>{args.large_file_mb} MiB' if not args.no_large_files else 'disabled',
               'fileWarning':file_warning,'findings':findings,'warnings':warnings}
        OUTPUT.write_text(generate_html(apps,context,audit),encoding='utf-8')
        print('\nSaved HTML:',OUTPUT)
        # Optional machine-readable snapshot, useful for investigating discrepancies.
        snapshot=OUTPUT.with_suffix('.json')
        snapshot.write_text(json.dumps({'apps':apps,'context':context,'audit':audit},
                                       indent=2,ensure_ascii=False),encoding='utf-8')
        print('Saved data snapshot:',snapshot)
        print('Audit is read-only. App, category and folder measurements can overlap.')
        if not args.no_open:
            webbrowser.open(OUTPUT.as_uri())
            print('Opening the report in your default browser.')
    except (RuntimeError,subprocess.TimeoutExpired) as exc:
        sys.exit('ADB scan failed: '+str(exc))


if __name__=='__main__':
    main()
