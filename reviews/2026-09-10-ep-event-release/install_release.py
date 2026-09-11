"""SG-only side-by-side install. Does not start units, send messages or alter the budget."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import tarfile

HOME = Path("/home/projects/quant")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("archive", type=Path)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--sec-contact-email", required=True)
    parser.add_argument("--upgrade-from", type=Path)
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+", args.sec_contact_email):
        raise ValueError("VALID_SEC_CONTACT_REQUIRED")
    if hashlib.sha256(args.archive.read_bytes()).hexdigest() != args.sha256:
        raise ValueError("ARCHIVE_HASH_MISMATCH")
    with tarfile.open(args.archive) as archive:
        members = archive.getmembers()
        names = [m.name for m in members]
        if len(set(names)) != len(names) or any(not m.isfile() or Path(m.name).is_absolute() or ".." in Path(m.name).parts for m in members):
            raise ValueError("UNSAFE_ARCHIVE")
        manifest_data = archive.extractfile("ep_release_manifest.json").read()
        manifest = json.loads(manifest_data)
        if set(names) != set(manifest) | {"ep_release_manifest.json"}:
            raise ValueError("MANIFEST_CONTENTS_MISMATCH")
        release = HOME / "releases" / ("ep-event-review-" + hashlib.sha256(manifest_data).hexdigest()[:16])
        if release.exists():
            raise ValueError("RELEASE_ALREADY_EXISTS")
        release.mkdir(parents=True)
        for member in members:
            data = archive.extractfile(member).read()
            if member.name in manifest and hashlib.sha256(data).hexdigest() != manifest[member.name]:
                raise ValueError("MEMBER_HASH_MISMATCH")
            target = release / member.name
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("xb") as handle:
                handle.write(data)
    # Configuration and units are new EP-specific paths, never replace existing deployments.
    unit_names = ["quant-ep-event-review-root.service", "quant-ep-event-review.timer"]
    paths = [Path("/etc/systemd/system") / name for name in unit_names]
    config_path = Path("/etc/quant/ep-event-worker.json")
    env_path = Path("/etc/quant/ep-event-worker.env")
    link = HOME / "ep-event-current"
    if args.upgrade_from:
        if not link.is_symlink() or link.resolve() != args.upgrade_from.resolve():
            raise ValueError("ACTIVE_RELEASE_CHANGED")
        for target in paths:
            if target.read_bytes() != (args.upgrade_from / "deploy/systemd" / target.name).read_bytes():
                raise ValueError("UNIT_CHANGED_OUTSIDE_RELEASE")
        for target in paths:
            target.write_bytes((release / "deploy/systemd" / target.name).read_bytes())
        temporary = link.with_name("ep-event-next-" + release.name)
        temporary.symlink_to(release, target_is_directory=True)
        os.replace(temporary, link)
        print(json.dumps({"release": str(release), "previous": str(args.upgrade_from), "configuration_preserved": True,
                          "service_started": False, "timer_enabled": False, "external_requests": 0}))
        return
    if any(p.exists() or p.is_symlink() for p in [*paths, config_path, env_path, link]):
        raise ValueError("EXISTING_EP_DEPLOYMENT_REQUIRES_EXPLICIT_UPDATE")
    (HOME / "data/ep").mkdir(parents=True, exist_ok=True)
    config = json.loads((release / "deploy/systemd/ep-event-worker.example.json").read_text())
    config["enabled"] = True
    config["jobs"] = [
        {"source_id": "4ee87559-2cdc-4917-b05f-1f5354a4054d", "paragraph_ids": ["p0001", "p0002", "p0008", "p0010", "p0012", "p0014"]},
        {"source_id": "38907f0d-b120-41dd-8d14-223815c8b86f", "paragraph_ids": ["p0002", "p0005", "p0006", "p0020"]},
    ]
    if not Path(config["database"]).is_file():
        raise ValueError("EXISTING_SHARED_BUDGET_DATABASE_REQUIRED")
    fd = os.open(config_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as output:
        json.dump(config, output, indent=2)
    fd = os.open(env_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as output:
        output.write("SEC_CONTACT_EMAIL=" + args.sec_contact_email + "\n")
    for target in paths:
        with target.open("xb") as output:
            output.write((release / "deploy/systemd" / target.name).read_bytes())
    link.symlink_to(release, target_is_directory=True)
    print(json.dumps({"release": str(release), "files_verified": len(manifest), "config": str(config_path),
                      "service_started": False, "timer_enabled": False, "external_requests": 0}))


if __name__ == "__main__":
    main()
