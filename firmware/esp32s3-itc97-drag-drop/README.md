# esp32s3-itc97

USB-Bildablage für ein 9,7"-Pervasive-iTC-Panel (960×672, Schwarz / Weiß / Rot) an einem ESP32-S3 mit 16 MB Flash und 8 MB PSRAM.

Ein USB-Stecker reicht. Der ESP öffnet ein Laufwerk. Eine Bilddatei darauf wird zugeschnitten und mit dem langen Panel-Update gezeichnet. Ob gedithert wird, steht in `CFG.TXT`. Es gibt keine Demo-Seiten.

## Ablauf

1. Der ESP meldet sich als USB-Massenspeicher **EPD97** (Produktname „EPD Drop“) und gleichzeitig als USB-Seriell für das Log. Eine Hardware-UART gibt es nicht.
2. Das Laufwerk ist eine FAT16-RAM-Disk, etwa 4 MB, nur im PSRAM. Nach einem Neustart ist sie leer. Nichts bleibt im Flash, außer einem per USB eingespielten Firmware-Update.
3. JPG, PNG oder BMP auf das Laufwerk ziehen. Nach etwa 1,5 Sekunden ohne weitere Schreibzugriffe nimmt der ESP die neueste Bilddatei.
4. Das Bild wird auf die volle Fläche 960×672 gelegt: eine Skalierung für beide Achsen, die längere Seite wird mittig abgeschnitten. Es wird nicht gestreckt und bekommt keinen weißen Rand.
5. `CFG.TXT` entscheidet das Dithering. `dithering 0` ist eine harte Schwelle bei 128, `dithering 1` ist Floyd-Steinberg. Fehlt die Zeile, gilt 0. Ein Neustart schreibt die Datei neu mit `dithering 0`. Speichern der Datei allein zeichnet ein schon gesendetes Bild nicht noch einmal.
6. Beide Ebenen (je 80640 Bytes) gehen über SPI ans Panel. Danach läuft das **lange LUT** und der ESP wartet auf BUSY. Das dauert am angeschlossenen Panel etwa 30–40 Sekunden. Das Kabel in der Zeit stecken lassen.
7. Die Bilddatei bleibt auf dem Stick. Löschen ist manuell. Dieselbe Datei wird nicht noch einmal gezeichnet, bis sie ersetzt wird (andere Größe, Zeit oder Inhalt).

Während das lange Update läuft, ist der Brownout-Reset aus. Das Panel zieht die Versorgung so weit herunter, dass der Chip sonst nach wenigen Sekunden neu startet und Windows den Stick als neues Gerät sieht.

## Bilder

| Format | Was gilt |
|---|---|
| JPEG | Baseline. Progressive JPEGs werden abgelehnt. Die EXIF-Orientierung wird angewandt, auch bei Hochkant-Fotos vom Handy, die in der Datei seitlich liegen. |
| PNG | Nicht interlaced. |
| BMP | Ungenpackt, Windows-Header, 1, 4, 8, 16, 24 oder 32 Bit pro Pixel. |

Jede Auflösung ist möglich. Zu große Bilder werden heruntergerechnet, zu kleine hochgerechnet, immer so, dass das Panel voll wird.

Rot gilt nur, wenn die Probe selbst rot ist: Rot mindestens 140, Grün und Blau höchstens 80, und Rot mindestens 90 über Grün und über Blau. Beim Dithern ersetzt Rot nur die dunklen Punkte der Fehlerverteilung. Die helle Seite bleibt Papier, deshalb wird eine rote Fläche gepunktet und nicht als volle Platte gezeichnet. Orange, Rosa und Braun bleiben schwarz oder weiß.

## Firmware über USB

`firmware.bin` oder `update.bin` auf **EPD97** legen. Der ESP prüft das Image (Magic `0xE9`, ESP32-S3, passt in den inaktiven OTA-Slot), schreibt es, setzt die Boot-Partition und startet neu. Eine abgelehnte Datei wird nicht erneut versucht, bis sie sich ändert.

Bauen:

```
pio run
```

Die Datei liegt danach unter `.pio/build/drop/firmware.bin`.

Der erste Flash eines leeren Chips geht über den ROM-Download-Modus (BOOT halten, Reset). Danach ist COM der App-CDC-Port, kein Bootloader: `esptool` auf diesem Port bekommt keine Daten. Updates laufen über die Datei auf dem Stick.

`platformio.ini` steht auf dem CDC-Port dieses Boards (derzeit COM23) und `upload_speed` 460800. Der Port kann sich ändern.

## Log

Das Log ist die native USB-Serielle Schnittstelle, 115200. Der ESP startet nicht neu, wenn das Terminal DTR oder RTS umschaltet.

Typische Zeilen:

```
drop: JPG, PNG or BMP, red on, dither on, long LUT
drop: the file stays on the stick after the refresh
image: … centre-crop … onto 960x672
image: quant dither  three_color 1  …
epd: slow waveform armed
epd: slow refresh done - panel … ms, frame … ms, red drawn
drop: … left on the stick
```

## Panel

960×672, zwei COGs an einem FPC. Jede Zeile geht als 60 Bytes zum Master, dann 60 Bytes zum Slave. Welche Variante steckt, liest der ESP einmal aus dem OTP:

| | E2969JS084 (driver 8) | E2969JS0B1 (driver B) |
|---|---|---|
| OTP-Öffner | `0xA8` | `0xB9` |
| Wellenform | aus dem OTP, lang | aus dem OTP, lang |
| kurzes LUT | wird von dieser Firmware nicht benutzt | wird von dieser Firmware nicht benutzt |

`epd_panel_name()` im Log nennt die gefundene Variante. Antwortet kein Panel, werden die Ebenen trotzdem über SPI geschickt. Die BUSY-Warte ist dann auf 2 Sekunden begrenzt, damit der Stick nicht hängen bleibt.

## Anschluss

Die Pins stehen in `include/epd97_config.h` und lassen sich über `build_flags` überschreiben.

| Display | ESP32-S3 | Hinweis |
|---|---|---|
| DC | GPIO10 | |
| SDA | GPIO47 | MOSI, wird für das OTP-Lesen umgedreht |
| SCL | GPIO21 | |
| CS1 | GPIO13 | Master, linke Hälfte |
| CS2 | GPIO3 | Slave, rechte Hälfte |
| Busy Master | GPIO12 | |
| Busy Slave | GPIO8 | `-DEPD_PIN_BUSY_S=-1`, wenn nicht verdrahtet |
| RST | GPIO11 | |
| VCC_EN | GPIO48 | Panel-Versorgung |

`EPD_PWR_ACTIVE_HIGH` ist 0. GPIO48 low schaltet das Panel ein (P-Kanal-High-Side). Ein aktives High wäre `-DEPD_PWR_ACTIVE_HIGH=1`. Falsch herum sieht das aus wie ein totes Panel: die OTP-Lesung kann trotzdem kurz klappen, der Refresh scheitert dann am Booster.

Register und OTP laufen bitweise bei etwa 50 kHz. Die beiden Bildebenen laufen über Hardware-SPI mit 4 MHz. MOSI und SCK werden dafür in der GPIO-Matrix umgehängt, die Chip-Selects bleiben GPIO.

## Was fest ist

- Rot ist immer an.
- Es läuft immer das lange LUT, mit Warten auf BUSY.
- Die Bilddatei wird nach dem Zeichnen nicht gelöscht.
- `CFG.TXT` hat nur `dithering`. Nach einem Neustart steht dort wieder `0`.
