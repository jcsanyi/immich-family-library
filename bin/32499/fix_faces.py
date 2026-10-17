#!/usr/bin/env python3
"""Repair faces on edited photos, a workaround for immich-app/immich#32499.

THE PROBLEM

When face detection runs on a photo that was edited in Immich (crop, rotate
or mirror), it stores the face boxes in the coordinates of the edited image.
The boxes then show in the wrong place. Detection also removes the faces that
the photo had before, together with their people.

This occurs when detection runs after the edit: the first detection on a
library that has edited photos, the face detection job on all assets, or
"Refresh faces" on one photo.

WHAT THE SCRIPT DOES

The script finds each edited photo in the library of one user. For each
photo, it runs face detection again and gives the new faces their people
back. It works on one photo at a time.

  1. It records the machine-learning faces of the photo and their people.
  2. It deletes those faces.
  3. In bug mode only, it removes the edits from the photo.
  4. It runs "Refresh faces" and waits for the new faces.
  5. In bug mode only, it puts the edits back.
  6. It gives each new face the person of the old face in the same place.

In bug mode, detection reads the unedited preview, so the boxes are correct.
In fixed mode, the edits stay on. Use fixed mode only on an Immich version
that has a fix for the bug.

WHAT YOU KEEP AND WHAT YOU LOSE

  - A face that had the correct box before keeps its person.
  - A face that the bug moved loses its person, because its box does not
    match the new face. Detection already removed the original person when
    the bug occurred. Recognition can give the new face a person again.
  - Manual faces and faces from file metadata do not change.
  - A face outside the crop is not visible through the API, so the script
    does not touch it. It keeps its person.
  - Hidden photos and photos in the locked folder are not processed.

You can run the script again. A second run does not change the result.

REQUIREMENTS

  - Python 3.8 or later. The script uses only the standard library.
  - Immich 3.3. The script was tested on v3.3.3.
  - Machine learning and face detection on.
  - An API key of the user who owns the photos. The script was tested with
    a key that has all permissions. To repair the photos of more than one
    user, run the script one time for each user.

HOW TO USE IT

  1. Do a dry run. It lists the edited photos and changes nothing:

       python3 fix_faces.py https://photos.example.com API_KEY --mode bug --dry-run

  2. While the bug is open, run bug mode:

       python3 fix_faces.py https://photos.example.com API_KEY --mode bug

  3. After a fix is released, you can run fixed mode:

       python3 fix_faces.py https://photos.example.com API_KEY --mode fixed

The script writes one line for each photo. The line gives the number of faces,
the people that it restored, and the people that it could not match.

The script waits up to --check-timeout (10 seconds by default) for the new
faces of a photo. If no faces come in that time, the photo goes to a list for
the end of the run. Detection finds no faces on some photos. An example is a
photo that was rotated in Immich, because its original is on its side. At the
end, the script checks the photos on the list one time. Then it waits one
--timeout and puts the edits back on each photo that is left.

--timeout (120 seconds by default) is also the limit for each other step on
one photo. If the machine-learning queue is long, increase --timeout.

SAFETY

CAUTION: Do not stop the script while it runs, if you can avoid it. If it
stops after it deletes the faces of a photo, the people of that photo are
lost. The script writes the names of these people when it stops.

Before bug mode removes the edits of a photo, it adds them to a backup file.
The file is edits-backup-<date>-<time>.jsonl, in the current directory. If
the script stops on an error or on Ctrl-C, it puts back the edits that it
removed. If that also fails, restore the edits from the backup file:

    python3 fix_faces.py https://photos.example.com API_KEY --restore FILE

The restore puts back the edits only on photos that have no edits now.

The exit code is 0 when all photos succeed, 1 when one or more fail, and 130
when you stop the script with Ctrl-C.
"""

import argparse
import datetime
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

POLL_SECONDS = 2
MATCH_IOU = 0.8


class ApiError(Exception):
    def __init__(self, method, path, status, message):
        super().__init__(f"{method} {path}: {status} {message}")
        self.status = status


class Api:
    def __init__(self, url, key):
        url = url.rstrip("/")
        self.base = url if url.endswith("/api") else url + "/api"
        self.key = key

    def call(self, method, path, body=None, query=None):
        url = self.base + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("x-api-key", self.key)
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=60) as res:
                raw = res.read()
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                message = json.loads(raw).get("message", raw.decode())
            except ValueError:
                message = raw.decode(errors="replace")
            raise ApiError(method, path, e.code, message) from None
        return json.loads(raw) if raw else None


class Failure(Exception):
    pass


def edited_assets(api):
    # Hidden photos are left out because detection skips them, and locked
    # ones because an API key can't read the locked folder.
    for visibility in ("timeline", "archive"):
        page = 1
        while page:
            body = {"page": page, "size": 1000, "visibility": visibility, "withDeleted": False}
            res = api.call("POST", "/search/metadata", body)["assets"]
            for asset in res["items"]:
                if asset.get("isEdited"):
                    yield asset
            page = int(res["nextPage"]) if res.get("nextPage") else None


def ml_faces(api, asset_id):
    faces = api.call("GET", "/faces", query={"id": asset_id})
    return [f for f in faces if f.get("sourceType") == "machine-learning"]


def get_edits(api, asset_id):
    res = api.call("GET", f"/assets/{asset_id}/edits")
    return [{"action": e["action"], "parameters": e["parameters"]} for e in res["edits"]]


def put_edits(api, asset_id, edits):
    api.call("PUT", f"/assets/{asset_id}/edits", {"edits": edits})


def has_edited_preview(api, asset_id):
    query = {"assetId": asset_id, "type": "preview", "isEdited": "true"}
    return bool(api.call("GET", "/asset-files", query=query))


def poll(check, timeout):
    """Call check until it returns something other than None, or time runs out."""
    deadline = time.monotonic() + timeout
    while True:
        value = check()
        if value is not None or time.monotonic() >= deadline:
            return value
        time.sleep(POLL_SECONDS)


def settled_faces(api, asset_id, timeout):
    """The photo's machine-learning faces, once there are some and two polls agree.

    Polling can't tell no faces from detection not done yet, so None means
    either one.
    """
    last = None

    def check():
        nonlocal last
        faces = ml_faces(api, asset_id)
        ids = sorted(f["id"] for f in faces)
        if ids and ids == last:
            return faces
        last = ids
        return None

    return poll(check, timeout)


def norm_box(face):
    # The API maps boxes through the edits, and gives the image size it maps
    # to. Fractions of that size compare the same whatever the preview size.
    w, h = face["imageWidth"] or 1, face["imageHeight"] or 1
    return (
        face["boundingBoxX1"] / w,
        face["boundingBoxY1"] / h,
        face["boundingBoxX2"] / w,
        face["boundingBoxY2"] / h,
    )


def iou(a, b):
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def match(recorded, faces):
    """Pair each recorded face with the new face over it, best overlap first, one to one."""
    pairs = sorted(
        (
            (iou(r["box"], norm_box(f)), i, j)
            for i, r in enumerate(recorded)
            for j, f in enumerate(faces)
        ),
        reverse=True,
    )
    used_r, used_f, out = set(), set(), []
    for score, i, j in pairs:
        if score < MATCH_IOU:
            break
        if i in used_r or j in used_f:
            continue
        used_r.add(i)
        used_f.add(j)
        out.append((recorded[i], faces[j]))
    return out


def person_label(r):
    return r["name"] or r["person_id"]


class Photo:
    def __init__(self, asset):
        self.id = asset["id"]
        self.label = f"{asset.get('originalFileName', '')} ({self.id})"
        self.recorded = []


class Fixer:
    def __init__(self, api, mode, timeout, check_timeout, backup_path):
        self.api = api
        self.bug = mode == "bug"
        self.timeout = timeout
        self.check_timeout = check_timeout
        self.backup_path = backup_path
        # Photos whose edits are off right now, so that an exit can put them back.
        self.removed = {}
        self.pending = []
        self.failed = 0

    def backup(self, asset_id, edits):
        line = json.dumps({"assetId": asset_id, "edits": edits})
        with open(self.backup_path, "a") as f:
            f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())

    def start(self, asset):
        photo = Photo(asset)
        try:
            self._start(photo)
        except Exception as e:
            self.fail(photo, e)
        except KeyboardInterrupt:
            self.fail(photo, "interrupted")
            raise

    def _start(self, photo):
        api = self.api
        faces = ml_faces(api, photo.id)
        photo.recorded = [
            {"box": norm_box(f), "person_id": f["person"]["id"], "name": f["person"].get("name")}
            for f in faces
            if f.get("person")
        ]
        # Old faces would absorb new detections that overlap them, and keep
        # their own boxes, which are wrong on a photo the bug hit.
        for f in faces:
            api.call("DELETE", f"/faces/{f['id']}", {"force": True})

        if self.bug:
            edits = get_edits(api, photo.id)
            self.backup(photo.id, edits)
            api.call("DELETE", f"/assets/{photo.id}/edits")
            self.removed[photo.id] = edits
            # Detection reads the edited preview whenever there is one.
            gone = poll(
                lambda: True if not has_edited_preview(api, photo.id) else None, self.timeout
            )
            if not gone:
                raise Failure("the edited preview is still there; detection did not run")

        api.call("POST", "/assets/jobs", {"assetIds": [photo.id], "name": "refresh-faces"})

        if not faces and not self.bug:
            print(f"queued     {photo.label}: no faces before; nothing to restore")
            return

        # A short wait here, because detection finds nothing on some photos.
        # A rotated photo's original can be on its side, for example. The
        # full timeout comes once, at the end, for all of them together.
        new = settled_faces(api, photo.id, self.check_timeout)
        if new is None:
            self.pending.append(photo)
            print(
                f"waiting    {photo.label}: no faces after {self.check_timeout}s; "
                "checked at the end"
            )
            return
        self.finish(photo)

    def finish(self, photo):
        try:
            self._finish(photo)
        except Exception as e:
            self.fail(photo, e)
        except KeyboardInterrupt:
            self.fail(photo, "interrupted")
            raise

    def _finish(self, photo):
        api = self.api
        if photo.id in self.removed:
            put_edits(api, photo.id, self.removed[photo.id])
            del self.removed[photo.id]
            # Until the edited preview is back, the faces outside the crop
            # still show, so the count would be off.
            poll(lambda: True if has_edited_preview(api, photo.id) else None, self.timeout)
        # Read the faces again after the edits are back, so that their boxes
        # are in the same coordinates as the recorded ones.
        faces = ml_faces(api, photo.id)
        pairs = match(photo.recorded, faces)
        restored, skipped = [], []
        for r, f in pairs:
            current = f.get("person") or {}
            if current.get("id") == r["person_id"]:
                restored.append(person_label(r))
                continue
            try:
                api.call("PUT", f"/faces/{r['person_id']}", {"id": f["id"]})
                restored.append(person_label(r))
            except ApiError as e:
                if e.status != 400:
                    raise
                skipped.append(f"{person_label(r)} (person gone)")
        matched = {id(r) for r, _ in pairs}
        lost = [person_label(r) for r in photo.recorded if id(r) not in matched]
        print(
            f"done       {photo.label}: {len(faces)} faces now; "
            f"restored {len(restored)}{list_suffix(restored)}; "
            f"not matched {len(lost)}{list_suffix(lost)}"
            + (f"; skipped {list_suffix(skipped)}" if skipped else "")
        )

    def fail(self, photo, error):
        self.failed += 1
        print(f"FAILED     {photo.label}: {error}", file=sys.stderr)
        if photo.recorded:
            names = ", ".join(person_label(r) for r in photo.recorded)
            print(f"           people on it before: {names}", file=sys.stderr)
        self.put_back(photo.id)

    def put_back(self, asset_id):
        edits = self.removed.pop(asset_id, None)
        if edits is None:
            return
        try:
            put_edits(self.api, asset_id, edits)
            print(f"           edits put back on {asset_id}", file=sys.stderr)
        except Exception as e:
            print(
                f"           COULD NOT put edits back on {asset_id}: {e}; "
                f"use --restore {self.backup_path}",
                file=sys.stderr,
            )

    def drain(self):
        if not self.pending:
            return
        print(f"\nchecking {len(self.pending)} photos that had no faces yet")
        waiting = []
        for photo in self.pending:
            if settled_faces(self.api, photo.id, POLL_SECONDS * 2) is not None:
                self.finish(photo)
            else:
                waiting.append(photo)
        if not waiting:
            return
        print(f"waiting {self.timeout}s for detection on {len(waiting)} photos")
        time.sleep(self.timeout)
        for photo in waiting:
            self.finish(photo)

    def put_back_all(self):
        for asset_id in list(self.removed):
            self.put_back(asset_id)


def list_suffix(items):
    return f" ({', '.join(items)})" if items else ""


def dry_run(api, bug):
    count = 0
    for asset in edited_assets(api):
        count += 1
        faces = ml_faces(api, asset["id"])
        people = [f["person"].get("name") or f["person"]["id"] for f in faces if f.get("person")]
        extra = f"; {len(get_edits(api, asset['id']))} edits" if bug else ""
        print(
            f"{asset.get('originalFileName', '')} ({asset['id']}): "
            f"{len(faces)} faces, {len(people)} with people{list_suffix(people)}{extra}"
        )
    print(f"\n{count} edited photos")


def restore(api, path):
    latest = {}
    with open(path) as f:
        for line in f:
            if line.strip():
                entry = json.loads(line)
                latest[entry["assetId"]] = entry["edits"]
    for asset_id, edits in latest.items():
        try:
            if get_edits(api, asset_id):
                print(f"skipped    {asset_id}: it has edits already")
                continue
            put_edits(api, asset_id, edits)
            print(f"restored   {asset_id}: {len(edits)} edits")
        except ApiError as e:
            print(f"FAILED     {asset_id}: {e}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0],
        epilog=__doc__.split("\n\n", 1)[1],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("url", help="Immich URL, for example https://photos.example.com")
    parser.add_argument("key", help="API key of the user whose library to fix")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--mode", choices=("bug", "fixed"))
    group.add_argument("--restore", metavar="FILE", help="put back the edits in a backup file")
    parser.add_argument(
        "--dry-run", action="store_true", help="list what would change, and change nothing"
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=120,
        help="seconds to wait for each step on one photo, and at the end (default 120)",
    )
    parser.add_argument(
        "--check-timeout",
        type=int,
        default=10,
        help="seconds to wait for the faces of one photo before it goes to the end (default 10)",
    )
    args = parser.parse_args()

    api = Api(args.url, args.key)
    me = api.call("GET", "/users/me")
    print(f"library of {me['name']} <{me['email']}> at {api.base}")

    if args.restore:
        restore(api, args.restore)
        return 0
    if args.dry_run:
        dry_run(api, args.mode == "bug")
        return 0

    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = os.path.abspath(f"edits-backup-{stamp}.jsonl")
    fixer = Fixer(api, args.mode, args.timeout, args.check_timeout, backup_path)
    if fixer.bug:
        print(f"edits backup: {backup_path}")
    print()

    try:
        # The list is read in full first, because removing and restoring edits
        # changes the photos while the search is still paging through them.
        assets = list(edited_assets(api))
        print(f"{len(assets)} edited photos\n")
        for asset in assets:
            fixer.start(asset)
        fixer.drain()
    finally:
        fixer.put_back_all()

    print(f"\n{len(assets)} photos, {fixer.failed} failed")
    return 1 if fixer.failed else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except ApiError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
