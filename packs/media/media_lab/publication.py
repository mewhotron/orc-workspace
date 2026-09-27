"""Prepare an export privately, then publish its complete directory in one rename.

Prepared records are recovery evidence, never completion markers. No source files
are moved or deleted. A final bundle includes all variants and its manifest.
"""
import hashlib
import json
import os
import tempfile
from pathlib import Path

from .errors import MediaLabError


def create_stage(destination, name):
    return Path(tempfile.mkdtemp(prefix=f'.{name}-', suffix='.pending', dir=destination))


def write_json(path, value):
    # Create a new inode exclusively. A predictable pre-existing .tmp pathname
    # could be a hard link to an original; never open one for writing.
    descriptor, filename = tempfile.mkstemp(prefix='.' + path.name + '-', suffix='.tmp', dir=path.parent)
    temporary = Path(filename)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        # Remove only the exclusive name owned by this call, never an existing
        # temporary pathname. A hard process kill can leave this diagnostic file.
        temporary.unlink(missing_ok=True)


def prepare_bundle(stage, provenance):
    write_json(stage / 'prepared.json', provenance)


def bundle_members(stage, provenance, name):
    final = stage.parent / (name + '.bundle')
    outputs = provenance.get('outputs', [])
    variants = [item.get('variant') for item in outputs]
    if not variants or len(set(variants)) != len(variants) or any(v not in ('landscape', 'vertical') for v in variants):
        raise MediaLabError('invalid_bundle', 'Prepared bundle has invalid variants.')
    members = []
    for output in outputs:
        target = final / f'{name}-{output["variant"]}.mp4'
        if Path(output['path']) != target:
            raise MediaLabError('invalid_bundle', 'Prepared output does not belong to this bundle.')
        complete = stage / target.name
        partial = complete.with_suffix('.partial.mp4')
        if complete.exists() == partial.exists():
            raise MediaLabError('invalid_bundle', 'Expected exactly one staged copy of each output.')
        candidate = complete if complete.exists() else partial
        if candidate.is_symlink() or not candidate.is_file():
            raise MediaLabError('invalid_bundle', 'Staged outputs must be regular local files.')
        members.append((candidate, complete, output))
    return final, members


def publish_bundle(stage, provenance, name):
    final, members = bundle_members(stage, provenance, name)
    if final.exists():
        raise MediaLabError('output_exists', 'Bundle already exists; no overwrite performed.')
    # All per-file changes occur inside a visibly unpublished staging directory.
    for candidate, complete, _ in members:
        if candidate != complete:
            candidate.rename(complete)
    write_json(stage / f'{name}-render.json', provenance)
    # Refuse a competing publication, including a pre-existing empty directory.
    if final.exists():
        raise MediaLabError('output_exists', 'Bundle appeared during publication; stage preserved.')
    stage.rename(final)
    return provenance


def recover_bundle(stage, edit, edit_sha256, validate, variants):
    """Explicit recovery: revalidate prepared outputs; never encode or delete them."""
    stage = Path(stage).absolute()
    name = edit['name']
    published = stage.name == name + '.bundle'
    pending = stage.name.startswith('.' + name + '-') and stage.name.endswith('.pending')
    if stage.is_symlink() or stage.resolve() != stage or not stage.is_dir() or not (published or pending):
        raise MediaLabError('invalid_bundle', 'Choose the original pending or published bundle for this edit.')
    raw = Path(__file__).resolve().parent.parent / 'media' / 'raw'
    sources = [edit['source']] if edit['schema_version'] == 1 else edit['sources'].values()
    if stage.is_relative_to(raw.resolve()) or any(stage.parent == Path(s['path']).resolve().parent for s in sources):
        raise MediaLabError('unsafe_output', 'Recovery cannot publish into original-source storage.')
    prepared = stage / 'prepared.json'
    if prepared.is_symlink():
        raise MediaLabError('invalid_bundle', 'Prepared evidence must not be a symbolic link.')
    try:
        provenance = json.loads(prepared.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise MediaLabError('recovery_unavailable', 'No complete prepared record; preserve this attempt and render a new job.') from exc
    if provenance.get('edit_sha256') != edit_sha256:
        raise MediaLabError('review_required', 'Recovery requires the exact originally reviewed edit.')
    final, members = bundle_members(stage, provenance, name)
    if set(variants) != {item[2]['variant'] for item in members}:
        raise MediaLabError('invalid_bundle', 'Recovery variants must match the prepared attempt.')
    if final.exists() and not published:
        raise MediaLabError('output_exists', 'Published bundle already exists; inspect its original result, do not overwrite.')
    if published:
        if any(candidate != complete for candidate, complete, _ in members):
            raise MediaLabError('invalid_bundle', 'Published bundles must contain every final-named output; partial members cannot be delivered.')
        manifest = stage / f'{name}-render.json'
        if manifest.is_symlink() or json.loads(manifest.read_text(encoding='utf-8')) != provenance:
            raise MediaLabError('invalid_bundle', 'Published manifest differs from the prepared evidence.')
    allowed = {'prepared.json', f'{name}-render.json', f'{name}-render.json.tmp'}
    for candidate, complete, _ in members:
        allowed.update((complete.name, complete.with_suffix('.partial.mp4').name))
    def allowed_member(path):
        orphan = path.name.startswith('.' + name + '-render.json-') and path.name.endswith('.tmp')
        return (path.name in allowed or orphan) and not path.is_symlink() and path.is_file()
    if any(not allowed_member(p) for p in stage.iterdir()):
        raise MediaLabError('invalid_bundle', 'Unexpected files in staging; preserve and inspect the attempt.')
    for candidate, _, output in members:
        with candidate.open('rb') as stream:
            checksum = hashlib.file_digest(stream, 'sha256').hexdigest()
        if checksum != output['sha256'] or candidate.stat().st_size != output['bytes']:
            raise MediaLabError('checksum_mismatch', 'Prepared output changed; recovery refused.')
        validate(candidate, output['variant'])
    # An interruption after the directory rename can lose the job result. Verify
    # the committed bundle and record a new recovery result without rewriting it.
    return provenance if published else publish_bundle(stage, provenance, name)
