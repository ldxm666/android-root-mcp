"""Root file/data access with atomic writes, metadata preservation and backups."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import posixpath
import re
import shlex
import sqlite3
import tarfile
import tempfile
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path


def package_name(value):
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+", value):
        raise ValueError("invalid Android package name")
    return value


def remote_path(value, allow_root=False):
    if not isinstance(value, str) or not value.startswith("/") or "\0" in value or "\n" in value:
        raise ValueError("device path must be absolute and contain no NUL/newline")
    normalized = posixpath.normpath(value)
    if normalized == "/" and not allow_root:
        raise ValueError("operation on '/' is not allowed by this high-level tool; use root_shell explicitly")
    return normalized


def validate_archive(archive: tarfile.TarFile, destination=None):
    """Do not let a phone archive escape a host/app restore directory."""
    members = archive.getmembers()
    for member in members:
        name = member.name.replace("\\", "/")
        if name.startswith("/") or re.match(r"^[A-Za-z]:", name) or ".." in name.split("/"):
            raise ValueError(f"unsafe archive member: {member.name}")
        if member.issym() or member.islnk() or member.isdev() or member.isfifo():
            raise ValueError("archive links/devices/FIFOs are not accepted by the high-level restore tool")
        if destination is not None:
            base = Path(destination).resolve()
            try:
                (base / name).resolve().relative_to(base)
            except ValueError as exc:
                raise ValueError("archive would escape destination through an existing symlink") from exc
    return members


def json_cell(value):
    if isinstance(value, bytes):
        return {"base64": base64.b64encode(value).decode("ascii"), "bytes": len(value)}
    return value


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024*1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class DataMixin:
    def read_remote_bytes(self, path, max_bytes=1_048_576, root=True):
        path = remote_path(path)
        if not 1 <= max_bytes <= 32 * 1024 * 1024:
            raise ValueError("max_bytes must be between 1 and 32 MiB")
        r = self.shell_result(f"head -c {max_bytes+1} {shlex.quote(path)}", root=root, max_bytes=min(max_bytes+1, 32*1024*1024), binary=True)
        if not r.success:
            raise RuntimeError(f"Cannot read device file: {r.as_dict()}")
        if len(r.stdout) > max_bytes or r.truncated:
            raise ValueError("file exceeds max_bytes; use file_transfer_root for a full transfer")
        return r.stdout

    def write_remote_bytes(self, path, data, root=True, mode="600", expected_sha256=None, backup=True):
        path = remote_path(path)
        if not re.fullmatch(r"[0-7]{3,4}", mode):
            raise ValueError("mode must be octal, e.g. 600 or 0644")
        if expected_sha256 and not re.fullmatch(r"[0-9a-fA-F]{64}", expected_sha256):
            raise ValueError("expected_sha256 must be a SHA-256 hex digest")
        token = uuid.uuid4().hex
        stage = f"/data/local/tmp/mcp_upload_{token}"
        temp = posixpath.join(posixpath.dirname(path), f".mcp_write_{token}")
        backup_path = path + ".mcp-backup-" + token if backup else None
        with tempfile.TemporaryDirectory(prefix="android_mcp_") as local:
            src = Path(local) / "payload"
            src.write_bytes(data)
            r = self.adb_result(["push", str(src), stage], timeout=120)
            if not r.success:
                return r.as_dict()
            q, qt, qs = map(shlex.quote, (path, temp, stage))
            script = ["trap " + shlex.quote(f"rm -f {qt} {qs}") + " EXIT", f"[ ! -L {q} ] || {{ echo 'Refusing symlink replacement' >&2; exit 72; }}"]
            if expected_sha256:
                script += [f"[ \"$(sha256sum {q} | cut -d' ' -f1)\" = {shlex.quote(expected_sha256.lower())} ] || {{ echo 'File changed since read' >&2; exit 73; }}"]
            script += [f"if [ -e {q} ]; then", f"  [ -f {q} ] || exit 74"]
            if backup:
                script.append(f"  cp -p {q} {shlex.quote(backup_path)} || exit $?")
            script += [f"  cp -p {q} {qt} || exit $?", f"  cat {qs} > {qt} || exit $?",
                       "else", f"  cat {qs} > {qt} && chmod {mode} {qt} || exit $?", "fi",
                       f"mv -f {qt} {q} || exit $?",
                       # Restorecon is best effort: not every filesystem/ROM supports it.
                       f"restorecon {q} 2>/dev/null || true", f"sha256sum {q}"]
            try:
                r = self.shell_result("\n".join(script), root=root, timeout=60)
            finally:
                self.shell_result("rm -f " + qs, root=root, timeout=5)
        return {**r.as_dict(), "path": path, "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(), "backup_path": backup_path if backup and r.success else None,
                "existing_metadata_preserved": True}

    def file_ops(self, action, path, destination="", text="", encoding="utf-8", mode="600", root=True,
                 recursive=False, expected_sha256=None, max_bytes=1_048_576, backup=True):
        path = remote_path(path, allow_root=action in ("list", "stat"))
        q = shlex.quote(path)
        if action == "read":
            data = self.read_remote_bytes(path, max_bytes=max_bytes, root=root)
            value = base64.b64encode(data).decode("ascii") if encoding == "base64" else data.decode(encoding)
            return {"success": True, "path": path, "content": value, "encoding": encoding,
                    "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        if action in ("write", "append"):
            data = base64.b64decode(text, validate=True) if encoding == "base64" else text.encode(encoding)
            if action == "append":
                original = self.read_remote_bytes(path, max_bytes=max_bytes, root=root)
                actual_hash = hashlib.sha256(original).hexdigest()
                if expected_sha256 and expected_sha256.lower() != actual_hash:
                    raise ValueError("File changed since read")
                expected_sha256 = actual_hash
                data = original + data
            if len(data) > 32 * 1024 * 1024:
                raise ValueError("use file_transfer_root for files over 32 MiB")
            return self.write_remote_bytes(path, data, root, mode, expected_sha256, backup)
        if action == "list":
            cmd = f"ls -la {q}"
        elif action == "stat":
            cmd = f"stat {q}"
        elif action == "hash":
            cmd = f"sha256sum {q}"
        elif action == "mkdir":
            if not re.fullmatch(r"[0-7]{3,4}", mode):
                raise ValueError("invalid octal mode")
            cmd = f"mkdir -p {q} && chmod {mode} {q}"
        elif action == "chmod":
            if not re.fullmatch(r"[0-7]{3,4}", mode):
                raise ValueError("invalid octal mode")
            cmd = f"chmod {'-R ' if recursive else ''}{mode} {q}"
        elif action in ("copy", "move"):
            dest = shlex.quote(remote_path(destination))
            cmd = f"{'cp -R -p' if recursive else 'cp -p'} {q} {dest}" if action == "copy" else f"mv {q} {dest}"
        elif action == "delete":
            cmd = f"rm {'-rf' if recursive else '-f'} {q}"
        else:
            raise ValueError("action must be list/stat/read/write/append/hash/mkdir/chmod/copy/move/delete")
        return {**self.shell_result(cmd, root=root, max_bytes=max_bytes).as_dict(), "path": path}

    def transfer(self, action, device_path, local_path, file_mode="644"):
        path = remote_path(device_path)
        local = Path(local_path).expanduser().resolve()
        q = shlex.quote(path)
        if not re.fullmatch(r"[0-7]{3,4}", file_mode):
            raise ValueError("invalid file_mode")
        if action == "pull":
            directory = self.shell_result(f"test -d {q}", root=True)
            if directory.exit_code not in (0, 1):
                return directory.as_dict()
            if directory.success:
                with tempfile.TemporaryDirectory() as tmp:
                    archive = Path(tmp) / "files.tar"
                    r = self.shell_result(f"tar -C {q} -cf - .", root=True, timeout=300, output_file=str(archive))
                    if not r.success:
                        return r.as_dict()
                    with tarfile.open(archive) as tf:
                        members = validate_archive(tf, local)
                        local.mkdir(parents=True, exist_ok=True)
                        tf.extractall(local, members=members, **({"filter": "data"} if hasattr(tarfile, "data_filter") else {}))
                return {"success": True, "directory": True, "local_path": str(local)}
            local.parent.mkdir(parents=True, exist_ok=True)
            temp = local.with_name(local.name + "." + uuid.uuid4().hex + ".part")
            try:
                r = self.shell_result(f"cat {q}", root=True, timeout=300, output_file=str(temp))
                if r.success:
                    os.replace(temp, local)
                return {**r.as_dict(), "local_path": str(local)}
            finally:
                temp.unlink(missing_ok=True)
        if action != "push":
            raise ValueError("action must be pull/push")
        if not local.exists():
            raise FileNotFoundError(str(local))
        if local.is_file() and local.stat().st_size <= 32 * 1024 * 1024:
            return self.write_remote_bytes(path, local.read_bytes(), mode=file_mode)
        stage = f"/data/local/tmp/mcp_transfer_{uuid.uuid4().hex}"
        with tempfile.TemporaryDirectory() as tmp:
            source = local
            is_dir = local.is_dir()
            if is_dir:
                source = Path(tmp) / "files.tar"
                with tarfile.open(source, "w") as tf:
                    tf.add(local, arcname=".")
                with tarfile.open(source) as tf:
                    validate_archive(tf)
            pushed = self.adb_result(["push", str(source), stage], timeout=300)
            if not pushed.success:
                return pushed.as_dict()
            try:
                if is_dir:
                    cmd = f"mkdir -p {q} && tar -xf {shlex.quote(stage)} -C {q}"
                else:
                    # Large files still replace atomically, preserving original ownership.
                    temp = shlex.quote(path + ".mcp-" + uuid.uuid4().hex)
                    cmd = f"if [ -f {q} ]; then cp -p {q} {temp}; else touch {temp} && chmod {file_mode} {temp}; fi && cat {shlex.quote(stage)} > {temp} && mv {temp} {q} && (restorecon {q} 2>/dev/null || true)"
                r = self.shell_result(cmd, root=True, timeout=300)
            finally:
                self.shell_result(f"rm -f {shlex.quote(stage)}", root=True)
        return {**r.as_dict(), "directory": is_dir, "device_path": path}

    def preferences(self, package, file, action="read", values=None, keys=None, storage="ce", user_id=0, stop_app=True):
        package = package_name(package)
        if not re.fullmatch(r"[\w.-]+", file) or file in (".", ".."):
            raise ValueError("file must be a simple SharedPreferences filename")
        if storage not in ("ce", "de") or not 0 <= user_id <= 10000:
            raise ValueError("invalid storage/user_id")
        if not file.endswith(".xml"):
            file += ".xml"
        root = f"/data/{'user' if storage == 'ce' else 'user_de'}/{user_id}/{package}/shared_prefs/{file}"
        if action not in ("read", "set", "delete"):
            raise ValueError("action must be read/set/delete")
        if action != "read":
            if not stop_app:
                raise ValueError("preference writes require stop_app=true to avoid cached values overwriting changes")
            edits = values or {} if action == "set" else {key: None for key in keys or []}
            if not edits:
                raise ValueError("set requires values; delete requires keys")
            # Validate all new typed values before changing the document.
            new_nodes = []
            for key, entry in edits.items():
                if not isinstance(key, str):
                    raise ValueError("preference keys must be strings")
                if action == "delete":
                    continue
                if not isinstance(entry, dict) or set(entry) != {"type", "value"}:
                    raise ValueError("each preference must be {type: ..., value: ...}")
                kind, value = entry["type"], entry["value"]
                if kind == "string" and isinstance(value, str):
                    node = ET.Element("string", name=key)
                    node.text = value
                elif kind == "set" and isinstance(value, list) and all(isinstance(x, str) for x in value):
                    node = ET.Element("set", name=key)
                    for s in dict.fromkeys(value):
                        ET.SubElement(node, "string").text = s
                elif kind == "boolean" and isinstance(value, bool):
                    node = ET.Element(kind, name=key, value=str(value).lower())
                elif kind in ("int", "long") and isinstance(value, int) and not isinstance(value, bool):
                    bits = 32 if kind == "int" else 64
                    if not -(2**(bits-1)) <= value < 2**(bits-1):
                        raise ValueError(f"{kind} out of range")
                    node = ET.Element(kind, name=key, value=str(value))
                elif kind == "float" and isinstance(value, (int, float)) and not isinstance(value, bool):
                    import math
                    if not math.isfinite(value):
                        raise ValueError("float must be finite")
                    node = ET.Element(kind, name=key, value=str(float(value)))
                else:
                    raise ValueError(f"invalid preference type/value for {key}")
                new_nodes.append(node)
            self.require_shell(f"am force-stop --user {user_id} {shlex.quote(package)}", root=True)
        data = self.read_remote_bytes(root)
        tree = ET.fromstring(data)
        if tree.tag != "map":
            raise ValueError("not a SharedPreferences XML map")
        if action == "read":
            parsed = {}
            for node in tree:
                name = node.get("name")
                if keys and name not in keys:
                    continue
                if node.tag == "string":
                    value = node.text or ""
                elif node.tag == "set":
                    value = [n.text or "" for n in node]
                elif node.tag == "boolean":
                    value = node.get("value") == "true"
                elif node.tag in ("int", "long"):
                    value = int(node.get("value", "0"))
                elif node.tag == "float":
                    value = float(node.get("value", "0"))
                else:
                    value = node.get("value")
                parsed[name] = {"type": node.tag, "value": value}
            return {"success": True, "path": root, "values": parsed, "sha256": hashlib.sha256(data).hexdigest()}
        for node in list(tree):
            if node.get("name") in edits:
                tree.remove(node)
        tree.extend(new_nodes)
        result = self.write_remote_bytes(root, ET.tostring(tree, encoding="utf-8", xml_declaration=True),
                                         expected_sha256=hashlib.sha256(data).hexdigest())
        return {**result, "package": package, "app_left_stopped": True}

    def sqlite(self, database, sql="", parameters=None, write=False, package="", stop_app=False, max_rows=200):
        database = remote_path(database)
        if not 1 <= max_rows <= 2000:
            raise ValueError("max_rows must be between 1 and 2000")
        if package:
            package_name(package)
        if write and (not stop_app or not package):
            raise ValueError("SQLite writes require package and stop_app=true")
        if stop_app:
            if not package:
                raise ValueError("stop_app requires package")
            self.require_shell("am force-stop " + shlex.quote(package), root=True)
        qdb = shlex.quote(database)
        stage = "/data/local/tmp/mcp_sqlite_" + uuid.uuid4().hex
        qs = shlex.quote(stage)
        with tempfile.TemporaryDirectory(prefix="android_mcp_sqlite_") as tmp:
            try:
                # Copy DB/WAL/SHM together in one remote invocation. A live copy remains
                # best effort: use stop_app for a stable application-owned database.
                self.require_shell(
                    f"mkdir -m 700 {qs} && cp -p {qdb} {qs}/main.db && "
                    f"for suffix in -wal -shm; do if [ -f {qdb}\"$suffix\" ]; then cp -p {qdb}\"$suffix\" {qs}/main.db\"$suffix\" || exit $?; fi; done",
                    root=True,
                )
                for suffix in ("", "-wal", "-shm"):
                    remote = f"{stage}/main.db{suffix}"
                    exists = self.shell_result("test -f " + shlex.quote(remote), root=True)
                    if not exists.success:
                        if suffix:
                            continue
                        return exists.as_dict()
                    result = self.shell_result("cat " + shlex.quote(remote), root=True, timeout=120,
                                               output_file=str(Path(tmp) / ("main.db" + suffix)))
                    if not result.success:
                        return result.as_dict()
                original_hash = self.require_shell(f"sha256sum {qs}/main.db", root=True).split()[0]
                local = Path(tmp) / "main.db"
                # Read-only URI + query_only prevent any accidental local mutation.
                source = sqlite3.connect(local.as_uri() + "?mode=ro", uri=True)
                source.execute("PRAGMA query_only=ON")
                connection = source
                try:
                    if write:
                        output = Path(tmp) / "updated.db"
                        connection = sqlite3.connect(output)
                        source.backup(connection)
                        connection.execute("PRAGMA journal_mode=DELETE")
                    query = sql.strip() or "SELECT name, type, sql FROM sqlite_master WHERE type IN ('table','view')"
                    cursor = connection.execute(query, parameters or [])
                    columns = [col[0] for col in cursor.description] if cursor.description else []
                    rows = cursor.fetchmany(max_rows + 1) if columns else []
                    result = {"success": True, "database": database, "columns": columns,
                              "rows": [[json_cell(v) for v in row] for row in rows[:max_rows]],
                              "returned_rows": min(len(rows), max_rows), "truncated": len(rows) > max_rows,
                              "consistency": "app_stopped" if stop_app else "best_effort_live_copy",
                              "write": write}
                    if write:
                        connection.commit()
                        result["affected_rows"] = cursor.rowcount
                finally:
                    if connection is not source:
                        connection.close()
                    source.close()
                if write:
                    backup_path = database + ".mcp-backup-" + uuid.uuid4().hex
                    # Back up all sidecars before installing a checkpointed replacement.
                    self.require_shell(f"mkdir -m 700 {shlex.quote(backup_path)} && cp -p {qs}/main.db* {shlex.quote(backup_path)}/", root=True)
                    replaced = self.write_remote_bytes(database, output.read_bytes(), expected_sha256=original_hash, backup=False)
                    if not replaced["success"]:
                        return {**replaced, "backup_path": backup_path, "app_left_stopped": True}
                    self.require_shell(f"rm -f {qdb}-wal {qdb}-shm", root=True)
                    result.update(backup_path=backup_path, app_left_stopped=True, sha256=replaced["sha256"])
                return result
            finally:
                self.shell_result(f"rm -rf {qs}", root=True, timeout=10)

    def app_data_archive(self, action, package, local_path, storage="ce", user_id=0):
        package = package_name(package)
        if storage not in ("ce", "de") or not 0 <= user_id <= 10000:
            raise ValueError("invalid storage/user_id")
        directory = f"/data/{'user' if storage == 'ce' else 'user_de'}/{user_id}/{package}"
        q = shlex.quote(directory)
        local = Path(local_path).expanduser().resolve()
        manifest_path = Path(str(local) + ".json")
        if action not in ("backup", "restore"):
            raise ValueError("action must be backup/restore")
        if action == "restore":
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if any(manifest.get(k) != v for k, v in {"package": package, "storage": storage, "user_id": user_id}.items()):
                raise ValueError("archive manifest does not match package/storage/user_id")
            if file_sha256(local) != manifest.get("sha256"):
                raise ValueError("archive SHA-256 does not match manifest")
            with tarfile.open(local) as tf:
                validate_archive(tf)
        # Quiesce the app; the tool intentionally leaves it stopped for caller-controlled restart.
        self.require_shell(f"am force-stop --user {user_id} {shlex.quote(package)}", root=True)
        if action == "backup":
            local.parent.mkdir(parents=True, exist_ok=True)
            r = self.shell_result(f"tar -C {q} -cf - .", root=True, timeout=300, output_file=str(local))
            if not r.success:
                return r.as_dict()
            with tarfile.open(local) as tf:
                try:
                    validate_archive(tf)
                except ValueError as exc:
                    return {"success": False, "archive": str(local), "error": str(exc),
                            "note": "Archive retained, but requires manual Root restoration; high-level restore rejects links/devices"}
            manifest = {"format": 1, "package": package, "storage": storage, "user_id": user_id,
                        "serial": self.serial, "sha256": file_sha256(local)}
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
            return {"success": True, "archive": str(local), "manifest": str(manifest_path),
                    "sha256": manifest["sha256"], "app_left_stopped": True}
        stage = "/data/local/tmp/mcp_restore_" + uuid.uuid4().hex
        backup = directory + ".mcp-before-restore-" + uuid.uuid4().hex
        r = self.adb_result(["push", str(local), stage], timeout=300)
        if not r.success:
            return r.as_dict()
        try:
            # Extract into a sibling, not over live files. Keep the original directory
            # for rollback; never replace the app's package-manager-owned directory.
            fresh = directory + ".mcp-staged-" + uuid.uuid4().hex
            uid_gid = self.require_shell(f"stat -c '%u:%g' {q}", root=True).strip()
            if not re.fullmatch(r"\d+:\d+", uid_gid):
                raise ValueError("unable to resolve installed app ownership")
            cmd = (f"mkdir -m 700 {shlex.quote(fresh)} && tar -xf {shlex.quote(stage)} -C {shlex.quote(fresh)} && "
                   f"chown -R {uid_gid} {shlex.quote(fresh)} && cp -a {q} {shlex.quote(backup)} && "
                   f"find {q} -mindepth 1 -maxdepth 1 -exec rm -rf {{}} \\; && "
                   f"cp -a {shlex.quote(fresh)}/. {q}/ && (restorecon -RF {q} 2>/dev/null || true)")
            r = self.shell_result(cmd, root=True, timeout=300)
            return {**r.as_dict(), "backup_path": backup, "app_left_stopped": True,
                    "note": "App data only; Keystore/TEE keys and external storage are not included"}
        finally:
            self.shell_result("rm -f " + shlex.quote(stage), root=True)
            if "fresh" in locals():
                self.shell_result("rm -rf " + shlex.quote(fresh), root=True)

    def inspect_app_data(self, package, mode="list", target_file="", sql_query="", pref_key=""):
        package = package_name(package)
        if mode == "sql_query":
            if not target_file or "/" in target_file or target_file in (".", ".."):
                raise ValueError("target_file must be a database filename")
            return self.sqlite(f"/data/user/0/{package}/databases/{target_file}", sql_query, package=package)
        if mode == "read_prefs" and target_file:
            result = self.preferences(package, target_file)
            if pref_key:
                result["values"] = {k: v for k, v in result["values"].items() if pref_key.casefold() in k.casefold()}
            return result
        if mode not in ("list", "read_prefs"):
            raise ValueError("mode must be list/read_prefs/sql_query")
        cmd = f"for d in /data/user/0/{package}/shared_prefs /data/user_de/0/{package}/shared_prefs /data/user/0/{package}/databases /data/user/0/{package}/files; do if [ -d \"$d\" ]; then echo \"=== $d ===\"; ls -la \"$d\"; fi; done"
        return {**self.shell_result(cmd, root=True, max_bytes=65536).as_dict(), "package": package}
