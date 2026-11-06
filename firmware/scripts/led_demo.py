#!/bin/env python3

# Import and initialize the pygame library
import asyncio
import math
import os
import sys

import pygame

sys.path.append(os.path.join(os.path.dirname(__file__), '../badge/lib/'))
import led_bar

pygame.init()

# Set up the drawing window
screen = pygame.display.set_mode([100 * led_bar.NUM_LEDS, 100])
screen.fill((255, 255, 255))

leds = [pygame.Surface((100, 100)) for _ in range(led_bar.NUM_LEDS)]
for i, led in enumerate(leds):
    screen.blit(led, (i * 100, 0))

def cylon():
    vals = []
    for _ in range(led_bar.NUM_LEDS):
        vals.append([0, 0, 0])
    while True:
        for i in range(1, led_bar.NUM_LEDS - 2):
            vals[i - 1] = (100, 0, 0)
            vals[i + 1] = (100, 0, 0)
            vals[i] = (200, 0, 0)
            yield vals, 200.0
            vals[i - 1] = (0, 0, 0)
        for i in range(led_bar.NUM_LEDS - 2, 1, -1):
            vals[i - 1] = (100, 0, 0)
            vals[i + 1] = (100, 0, 0)
            vals[i] = (200, 0, 0)
            yield vals, 200.0
            vals[i + 1] = (0, 0, 0)

def rainbow():
    vals = []
    for _ in range(led_bar.NUM_LEDS):
        vals.append([0, 0, 0])
    i = 0
    while True:
        for led in range(led_bar.NUM_LEDS):
            vals[led] = (
                int(127 * (math.sin((i + led * 2) * 3.14 / 180) + 1)),
                int(127 * (math.sin((i + led * 2 + 120) * 3.14 / 180) + 1)),
                int(127 * (math.sin((i + led * 2 + 240) * 3.14 / 180) + 1))
            )
        yield vals, 100
        i += 1
        i = i % 360

def example_sequence():
    # Create an array of LED values to populate
    vals = []
    for _ in range(led_bar.NUM_LEDS):
        vals.append([0, 0, 0])
    # Run the sequence forever
    i = 0
    while True:
        # Assign each LED. Careful, they're bright!
        for n, led in enumerate(vals):
            led[0] = ((i + n) % 2) * 60  # red
            led[1] = ((i + n) % 3) * 40  # green
            led[2] = ((i + n) % 4) * 30  # blue
        # yield (like return, but the function keeps going)
        # the values to set to the LEDs, and how many milliseconds
        # to delay before getting the next values
        yield vals, 1000.0
        # Increment the counter so the values change next cycle
        i += 1

def r(x, v=255):
    """Bitmask of red LEDs"""
    return led_bar.bitmask_to_leds(x, r=v, g=0, b=0)

def g(x, v=255):
    """Bitmask of green LEDs"""
    return led_bar.bitmask_to_leds(x, r=0, g=v, b=0)

def b(x, v=255):
    """Bitmask of blue LEDs"""
    return led_bar.bitmask_to_leds(x, r=0, g=0, b=v)

def w(x, v=255):
    """Bitmask of white LEDs"""
    return led_bar.bitmask_to_leds(x, r=v, g=v, b=v)

def swipe():
    i = 1
    while True:
        yield w(i), 100
        i *= 2
        if i >= 0xffff:
            i = 1

def wizards():
    """https://www.youtube.com/watch?v=IK90Ys2LhSo"""
    beat = 206  # 1/8th note
    yield w(0x00), beat
    while True:
        # 00:00
        for a in [
            [0x80,0x40,0x20,0x10,0x08,0x04,0x02],
            [0x10,0x08,0x04,0x02,0x04,0x08,0x10]
        ]:
            for i in range(6):
                for _ in range(3):
                    yield w(0x18, 32 * (i+1)), beat / 2
                    yield w(0x00), beat / 2
                yield w(0x24, 32 * (i+1)), beat
            yield w(0x18, 255), beat
            for c in a:
                yield w(c), beat
        # 00:13
        for _ in range(6):
            yield r(0x0C), beat * 2
            yield r(0x30), beat * 2
        for a in [0x24,0x80,0x40,0x20,0x10,0x08,0x04,0x02]:
            yield r(a), beat
        for _ in range(6):
            yield g(0x0C), beat * 2
            yield g(0x30), beat * 2
        for a in [0x18,0x40,0x20,0x10,0x08]:
            yield b(a), beat / 2
            yield b(0x00), beat / 2
        yield w(0xFF), beat
        for a in [0x40,0x20,0x10,0x0]:
            yield w(a), beat / 2
            yield w(0x00), beat / 2
        yield r(0xFF), beat

led_bar.leds.set_sequence(wizards)

async def main():
    asyncio.create_task(led_bar.leds.update())

    # Run until the user asks to quit
    led_bar.leds.pause()
    running = True
    while running:

        # Did the user click the window close button?
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False
            elif event.type == pygame.KEYDOWN:
                print(event.key)
                if event.key == 32:
                    led_bar.leds.toggle_pause()

        for i, (val, disp_led) in enumerate(zip(led_bar.leds.write(), leds)):
            disp_led.fill(val)
            screen.blit(disp_led, (i * 100, 0))

        # Flip the display
        pygame.display.flip()
        await asyncio.sleep(1 / 120)

    # Done! Time to quit.
    pygame.quit()

asyncio.run(main())