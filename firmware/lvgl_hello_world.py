import lvgl
import gc


lvgl.init()

## this fails with an absurd memory allocation the second time you run it
## be sure to run a machine.reset() or press the button first
display = lvgl.display_create(960, 672)


display.set_color_format(lvgl.COLOR_FORMAT.RGB565)
## lvgl.COLOR_FORMAT.I2 is a two-bit greyscale.  I don't know if we want to abuse it for our purposes

# lvgl_color_framebuffer = bytearray(960 * 672 * 2)
# display.set_draw_buffers( lvgl_color_framebuffer,  lvgl.DISPLAY_RENDER_MODE.FULL)
## fails -- need lvgl.draw_buf_create()
# display.set_flush_cb( todo )

if False:
    print(gc.mem_free())
    ## image test
    screen = lvgl.screen_active()
    test_filename = "data/assets/Screw-dithered.png"
    image = lvgl.image(screen)
    with open(test_filename, "rb") as f:
        image_data = f.read()
    image_dsc = lvgl.image_dsc_t({"data_size": len(image_data), "data": image_data})
    image.set_src(image_dsc)
    image.align(lvgl.ALIGN.CENTER, 0, 0)

    print(gc.mem_free())
    ## 48 kB -- probably just the PNG, not decoded yet.  assume done at runtime when rendered?


if False:
    # Create a button
    btn = lvgl.button(screen)
    btn.align(lvgl.ALIGN.CENTER, 0, 0)
    
    # Create a label on the button
    label = lvgl.label(btn)
    label.set_text("Hello World")
    
    # Load the screen -- should render it into the display's image buffer
    lvgl.screen_load(screen)

    ## not sure if this renders automatically to flush callback or not
    ## might need update?  

