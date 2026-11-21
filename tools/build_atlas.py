"""Build map artwork and coordinate metadata from the locally installed Classic client.

Run with the development-only mpyq package installed. Game files are read only.
"""
import argparse
from contextlib import ExitStack
import io
import json
from pathlib import Path
import struct

from PIL import Image


def dbc(path, fields):
    raw=path.read_bytes()
    if len(raw)<20:
        raise ValueError(f'Truncated Classic database: {path.name}')
    magic,count,actual,size,strings=struct.unpack_from('<4s4I',raw)
    if magic!=b'WDBC' or actual!=fields or size!=4*fields or len(raw)!=20+count*size+strings:
        raise ValueError(f'Expected the original Classic {path.name}.')
    return raw[20:20+count*size],raw[20+count*size:]


def string_at(text, offset):
    return text[offset:].split(b'\0',1)[0].decode()


def draw_map(texture, overlays, read):
    image=Image.new('RGBA',(1024,768))
    directory=rf'Interface\WorldMap\{texture}'
    for i in range(12):
        path=rf'{directory}\{texture}{i+1}.blp'
        data=read(path)
        if not data:
            raise ValueError(f'Missing map tile {path}')
        with Image.open(io.BytesIO(data)) as tile:
            image.paste(tile.convert('RGBA'),(i%4*256,i//4*256))
    for name,width,height,left,top in overlays:
        columns=(width+255)//256
        for y in range(0,height,256):
            for x in range(0,width,256):
                path=rf'{directory}\{name}{(y//256)*columns+x//256+1}.blp'
                data=read(path)
                if not data:
                    # Exploration metadata can name art absent from the original archive.
                    continue
                with Image.open(io.BytesIO(data)) as tile:
                    visible=tile.convert('RGBA').crop((0,0,min(256,width-x),min(256,height-y)))
                    image.alpha_composite(visible,(left+x,top+y))
    return image.crop((0,0,1002,668)).convert('RGB')


def build(client, output, *, data_root=None, catalog_path=None):
    import numpy as np
    from mpyq import MPQArchive

    dbcs=(data_root or client)/'dbc'
    records,text=dbc(dbcs/'WorldMapArea.dbc',8)
    rows={}
    atlas_continents={}
    for record,continent,area,name,*bounds in struct.iter_unpack('<4I4f',records):
        if continent not in (0,1):
            continue
        texture=string_at(text,name)
        row=dict(id=area,continent=continent,texture=texture,bounds=bounds)
        if area:
            rows[area]=(row,record)
        else:
            atlas_continents[str(continent)]=dict(row,name='Eastern Kingdoms' if continent==0 else 'Kalimdor',
                                                  file=f'{texture}.jpg')
    zones=json.loads(catalog_path.read_text(encoding='utf-8')) if catalog_path else []
    overlays={}
    if zones:
        records,text=dbc(dbcs/'WorldMapOverlay.dbc',17)
        for record in struct.iter_unpack('<17I',records):
            name=string_at(text,record[8])
            if name and record[9] and record[10]:
                overlays.setdefault(record[1],[]).append((name,*record[9:13]))
        missing={zone['id'] for zone in zones}-rows.keys()
        if missing:
            raise ValueError(f'Classic map assignments missing for areas {sorted(missing)}.')

    output.mkdir(parents=True,exist_ok=True)
    highlights=output/'highlights'
    highlights.mkdir(exist_ok=True)
    data={path.name.casefold():path for path in (client/'Data').glob('*') if path.is_file()}
    if 'interface.mpq' not in data:
        raise ValueError('The Classic client is missing Data/interface.MPQ.')
    with ExitStack() as stack:
        archives=[]
        for name in ('patch-2.mpq','patch.mpq','interface.mpq'):
            if name in data:
                archive=MPQArchive(str(data[name]),listfile=False)
                stack.callback(archive.file.close)
                archives.append(archive)

        def read(path):
            return next((contents for archive in archives if (contents:=archive.read_file(path))),None)

        for texture in ('Azeroth','Kalimdor','World'):
            draw_map(texture,[],read).save(output/f'{texture}.jpg',quality=94)
        for zone in zones:
            row,record=rows[zone['id']]
            draw_map(row['texture'],overlays.get(record,[]),read).save(output/zone['file'],quality=94)
        for area,(row,_) in rows.items():
            name=row['texture']
            contents=read(rf'Interface\WorldMap\{name}\{name}Highlight.blp')
            if contents:
                with Image.open(io.BytesIO(contents)) as mask:
                    rgb=np.asarray(mask.convert('RGB'))
                filename=f'highlights/{area}.png'
                Image.fromarray(rgb.max(axis=2)).save(output/filename)
                row['highlight']=filename
    if zones:
        (output/'catalog.json').write_text(json.dumps(zones,indent=2),encoding='utf-8')
    metadata=dict(version=1,continents=atlas_continents,zones={str(area):row for area,(row,_) in rows.items()})
    (output/'atlas.json').write_text(json.dumps(metadata,indent=2),encoding='utf-8')
    print(f'Built {len(zones)} Classic zone maps and three overviews.',flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--client',type=Path,default=Path(r'C:\World of Warcraft Classic'))
    parser.add_argument('--output',type=Path,default=Path(__file__).resolve().parents[1]/'assets/maps')
    args=parser.parse_args()
    build(args.client,args.output)
