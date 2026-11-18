"""Build a compact, local fsaverage cortical illustration from Nilearn GIFTI files.

Usage: python scripts/build_brain_surface.py /path/to/gifti/files
See static/VENDORING.md for pinned sources and licenses. No patient data.
"""
from __future__ import annotations
import base64
import gzip
import json
from pathlib import Path
import sys
import xml.etree.ElementTree as ET
import zlib
import numpy as np
from scipy.spatial import cKDTree


def arrays(path: Path) -> list[np.ndarray]:
    result = []
    for a in ET.fromstring(gzip.decompress(path.read_bytes())).findall('DataArray'):
        dtype = '<f4' if a.attrib['DataType'] == 'NIFTI_TYPE_FLOAT32' else '<i4'
        shape = tuple(int(a.attrib['Dim' + str(i)]) for i in range(int(a.attrib['Dimensionality'])))
        result.append(np.frombuffer(zlib.decompress(base64.b64decode(a.findtext('Data'))), dtype=dtype).reshape(shape))
    return result


def build(source: Path, root: Path) -> Path:
    vertices, triangles, sulci = [], [], []
    for side in ['left', 'right']:
        coords, faces = arrays(source / f'pial_{side}.gii.gz')
        sphere = arrays(source / f'sphere_{side}.gii.gz')[0]
        sulc = arrays(source / f'sulc_{side}.gii.gz')[0]
        # fsaverage5 is an icosphere subdivision: the first 2562 nodes are ico4.
        # Collapse in spherical space so opposing sulcal banks do not get merged.
        count = 2562
        mapping = cKDTree(sphere[:count]).query(sphere)[1]
        coarse = mapping[faces]
        coarse = coarse[(coarse[:,0] != coarse[:,1]) & (coarse[:,0] != coarse[:,2]) & (coarse[:,1] != coarse[:,2])]
        _, indices = np.unique(np.sort(coarse, axis=1), axis=0, return_index=True)
        coarse = coarse[np.sort(indices)]
        offset = len(vertices)
        # FreeSurfer RAS -> x right, y superior, z posterior, normalized to scalp.
        center = np.array([0., -19., 14.])
        coords = (coords[:count] - center) / 100.
        vertices.extend(np.round(coords[:, [0,2,1]] * [1,1,-1], 4).tolist())
        triangles.extend((coarse + offset).tolist())
        sulci.extend(np.round(sulc[:count], 3).tolist())
    model = {'vertices': vertices, 'triangles': triangles, 'sulci': sulci}
    license_text = (root / 'static/vendor/freesurfer-LICENSE.txt').read_text()
    nilearn_license = (root / 'static/vendor/nilearn-LICENSE.txt').read_text()
    output = root / 'static/brain_surface.js'
    output.write_text('/* Modified, downsampled fsaverage5 pial surface, obtained via Nilearn.\n'
        'Generic anatomical illustration; not an individual reconstruction.\n'
        'All or portions of this licensed product (such portions are the "Software") '
        'have been obtained under license from The General Hospital Corporation "MGH" '
        'and are subject to the following terms and conditions:\n' + license_text + '\n' + nilearn_license
        + '\n*/\nwindow.NeuroBrainSurface=' + json.dumps(model, separators=(',', ':')) + ';\n')
    print(f'{len(vertices)} vertices, {len(triangles)} triangles, {output.stat().st_size} bytes')
    return output


if __name__ == '__main__':
    build(Path(sys.argv[1]), Path(__file__).resolve().parents[1])
