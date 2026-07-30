{ writers }: writers.writePython3Bin "update" { } (builtins.readFile ./update.py)
