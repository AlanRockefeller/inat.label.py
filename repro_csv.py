import csv
import io

csv_content = """Scientific Name,Common Name,Habitat,Spore Print,Edibility
Tubaria furfuracea,scurfy twiglet,on wood,orange,unknown"""

f = io.StringIO(csv_content)
reader = csv.DictReader(f)

header_map = {}
if reader.fieldnames:
    for field in reader.fieldnames:
        clean_field = field.strip().lower().replace(' ', '').replace('_', '')
        header_map[clean_field] = field

def get_val(row, keys):
    for key in keys:
        real_key = header_map.get(key)
        # Mimicking the logic in inat.label.py
        if real_key and row.get(real_key):
            return row[real_key].strip()
    return None

for row in reader:
    print(f"Row raw: {row}")
    edibility = get_val(row, ['edibility'])
    print(f"Edibility extracted: '{edibility}'")
