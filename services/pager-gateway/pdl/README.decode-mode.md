# PDL RS232 decode mode

For the discriminator.nl FSK-to-USB interface, PDL's `DecodeMode` follows the legacy PDW timing modes:

- `1` = POCSAG
- `2` = FLEX 1600
- `3` = Mobitex 8000

Racher Pager uses `DecodeMode=1` for the POCSAG FSK-USB path. The POCSAG baud selection (512/1200/2400) remains controlled separately by the PDL POCSAG settings.
