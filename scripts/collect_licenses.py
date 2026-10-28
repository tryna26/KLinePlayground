"""Collect complete dependency notices from the locked build environment."""
from importlib import metadata
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]


def main():
    destination=ROOT/'build/licenses'
    destination.mkdir(parents=True,exist_ok=True)
    distributions={}
    for dist in metadata.distributions():
        name=dist.metadata['Name']
        if name.lower() in {'pip'}:continue
        distributions[name]=dist.version
        target=destination/name
        target.mkdir(exist_ok=True)
        found=False
        for item in dist.files or []:
            if any(word in str(item).lower() for word in ('license','copying','notice','copyright','authors')):
                source=dist.locate_file(item)
                if source.is_file() and '.dist-info/' in str(item):
                    relative=Path(*Path(item).parts[1:])
                    output=target/relative
                    output.parent.mkdir(parents=True,exist_ok=True)
                    shutil.copyfile(source,output)
                    found=True
        # Some distributions publish their complete license in METADATA.
        text=dist.read_text('METADATA')
        if text and not found:(target/'METADATA.txt').write_text(text,encoding='utf-8')
    python_license=Path(sys.base_prefix)/'LICENSE.txt'
    if not python_license.is_file():raise RuntimeError('Python license missing')
    shutil.copyfile(python_license,destination/'PYTHON-LICENSE.txt')
    for path in (ROOT/'licenses').glob('*'):
        if path.is_file():shutil.copyfile(path,destination/path.name)
    (destination/'VERSIONS.txt').write_text('\n'.join(f'{name}=={version}' for name,version in sorted(distributions.items())),encoding='utf-8')
    print(f'Collected notices for {len(distributions)} distributions')


if __name__=='__main__':main()
