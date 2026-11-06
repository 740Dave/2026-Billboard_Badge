// Include MicroPython API.
#include "py/runtime.h"
#include "py/obj.h"

// #define LODEPNG_NO_COMPILE_ENCODER
// #define LODEPNG_NO_COMPILE_DISK
// #define LODEPNG_COMPILE_DECODER
#include "lodepng.h"
// #include "../../lv_micropython/user_modules/lv_binding_micropython/lvgl/lvgl.h"
#include "lvgl.h"

// This is the function which will be called from Python as lodepng.decode_png(file_data).
static mp_obj_t png_decode_filtered(mp_obj_t file_data, mp_obj_t framebufs_obj, mp_obj_t top_left_obj)
{
    // Extract bytes from file_data
    mp_buffer_info_t file_info;
    mp_get_buffer(file_data, &file_info, MP_BUFFER_READ);

    // Extract mutable buffers
    size_t framebufs_len = 0;
    mp_obj_t *framebufs;
    mp_obj_tuple_get(framebufs_obj, &framebufs_len, &framebufs);
    if (framebufs_len != 2)
    {
        mp_raise_msg(&mp_type_ValueError, MP_ERROR_TEXT("framebufs must be tuple of 2 framebufs (black, red)"));
    }

    mp_buffer_info_t black_info;
    mp_buffer_info_t red_info;

    mp_get_buffer(framebufs[0], &black_info, MP_BUFFER_WRITE);
    mp_get_buffer(framebufs[1], &red_info, MP_BUFFER_WRITE);

    size_t coords_len = 0;
    mp_obj_t *top_left_objs;
    mp_obj_tuple_get(top_left_obj, &coords_len, &top_left_objs);
    if (coords_len != 2)
    {
        mp_raise_msg(&mp_type_ValueError, MP_ERROR_TEXT("top_left must be tuple of 2 ints (x, y)"));
    }
    size_t dpy_top_pxl = mp_obj_get_int(top_left_objs[0]);
    size_t dpy_left_pxl = mp_obj_get_int(top_left_objs[1]);
    printf("%i, %i", dpy_top_pxl, dpy_left_pxl);

    // Initialize output pointers for lodepng
    unsigned img_w = 0, img_h = 0;
    unsigned char *image = NULL;

    LodePNGState state;
    lodepng_state_init(&state);
    state.info_raw.colortype = LCT_PALETTE;
    state.info_raw.bitdepth = 8;

    // Decode with palette colortype to populate state.info_png.color
    unsigned error = 0;
    error = lodepng_decode(&image, &img_w, &img_h, &state,
                           (const unsigned char *)file_info.buf, file_info.len);
    if (error == 82)
    {
        // Image is only grayscale, not color-paletted
        state.info_raw.colortype = LCT_GREY;
        state.info_raw.bitdepth = 1;
        error = lodepng_decode(&image, &img_w, &img_h, &state,
                               (const unsigned char *)file_info.buf, file_info.len);
    }
    else if (error == 56)
    {
        lodepng_state_cleanup(&state);
        mp_raise_msg_varg(&mp_type_ValueError,
                          MP_ERROR_TEXT("PNG decode error %u: %s"), error, lodepng_error_text(error));
    }
    if (error)
    {
        lodepng_state_cleanup(&state);
        mp_raise_msg_varg(&mp_type_ValueError,
                          MP_ERROR_TEXT("PNG decode error %u: %s"), error, lodepng_error_text(error));
    }

    // Verify buffers are large enough
    size_t needed_bytes = (img_w * img_h + 7) / 8; // For 1-bit-per-pixel output
    if (black_info.len < needed_bytes || red_info.len < needed_bytes)
    {
        free(image);
        lodepng_state_cleanup(&state);
        mp_raise_msg(&mp_type_ValueError, MP_ERROR_TEXT("Output buffers too small"));
    }

    unsigned char *black_data = (unsigned char *)black_info.buf;
    unsigned char *red_data = (unsigned char *)red_info.buf;

    // Clear output buffers
    // memset(black_data, 0, needed_bytes);
    // memset(red_data, 0, needed_bytes);

    size_t dpy_pxl_w = 960;
    size_t dpy_pxl_h = 672;
    // Image row and col, display row and col pixel indexes (will be converted to byte:bit)
    size_t img_pxl_idx, img_byte_idx, img_bit_idx;
    size_t dpy_pxl_idx, dpy_pxl_r, dpy_pxl_c, dpy_byte_idx, dpy_bit_idx;
    uint8_t pxl_black = 0, pxl_red = 0;

    if (state.info_raw.colortype == LCT_PALETTE)
    {
        LodePNGColorMode *color = &state.info_png.color;

        // Palette is stored as flat RGB/RGBA bytes
        unsigned char *palette = color->palette;
        // unsigned palette_size = color->palettesize; // number of entries

        // Iterate over palette indices and filter into bitmaps
        // With 2-bit depth, each byte contains 255 palette indices (8 bits each)
        // Lodepng returns error 56 if colortype == palette and bits != 8
        unsigned char palette_idx, r, g, b;
        for (size_t img_pxl_r = 0; img_pxl_r < img_h; img_pxl_r++)
        {
            for (size_t img_pxl_c = 0; img_pxl_c < img_w; img_pxl_c++)
            {
                img_pxl_idx = img_pxl_r * img_w + img_pxl_c;
                palette_idx = image[img_pxl_idx];

                r = palette[palette_idx * 4 + 0]; // Red
                g = palette[palette_idx * 4 + 1]; // Green
                b = palette[palette_idx * 4 + 2]; // Blue
                // unsigned char a = palette[i * 4 + 3];  // Alpha (if present)

                // Key each color off half brightness, as indicated by the top bit
                pxl_red = (r & 0x80) && !(g & 0x80) && !(b & 0x80);
                pxl_black = (!(r & 0x80) && !(g & 0x80) && !(b & 0x80));
                // Everything else becomes white

                dpy_pxl_r = img_pxl_r + dpy_top_pxl;
                dpy_pxl_c = img_pxl_c + dpy_left_pxl;
                if ((dpy_pxl_r >= dpy_pxl_h) || (dpy_pxl_c >= dpy_pxl_w))
                {
                    continue;
                }
                dpy_pxl_idx = dpy_pxl_r * dpy_pxl_w + dpy_pxl_c;
                dpy_byte_idx = dpy_pxl_idx / 8;
                dpy_bit_idx = 7 - (dpy_pxl_idx % 8);
                if (pxl_black)
                {
                    black_data[dpy_byte_idx] |= (1 << dpy_bit_idx);
                }
                else
                {
                    black_data[dpy_byte_idx] &= ~(1 << dpy_bit_idx);
                }
                if (pxl_red)
                {
                    red_data[dpy_byte_idx] |= (1 << dpy_bit_idx);
                }
                else
                {
                    red_data[dpy_byte_idx] &= ~(1 << dpy_bit_idx);
                }
            }
        }
    }
    else if (state.info_raw.colortype == LCT_GREY)
    {
        // Both image and black framebuf are 1 bit per pixel, but
        // in the image white is 1 and black is 0, so invert all the bits.

        for (size_t img_pxl_r = 0; img_pxl_r < img_h; img_pxl_r++)
        {
            for (size_t img_pxl_c = 0; img_pxl_c < img_w; img_pxl_c++)
            {
                img_pxl_idx = img_pxl_r * img_w + img_pxl_c;
                img_byte_idx = img_pxl_idx / 8;
                img_bit_idx = 7 - (img_pxl_idx % 8);

                pxl_black = !((image[img_byte_idx] >> img_bit_idx) & 1);

                dpy_pxl_r = img_pxl_r + dpy_top_pxl;
                dpy_pxl_c = img_pxl_c + dpy_left_pxl;
                if ((dpy_pxl_r >= dpy_pxl_h) || (dpy_pxl_c >= dpy_pxl_w))
                {
                    continue;
                }
                dpy_pxl_idx = dpy_pxl_r * dpy_pxl_w + dpy_pxl_c;
                dpy_byte_idx = dpy_pxl_idx / 8;
                dpy_bit_idx = 7 - (dpy_pxl_idx % 8);
                if (pxl_black)
                {
                    black_data[dpy_byte_idx] |= (1 << dpy_bit_idx);
                }
                else
                {
                    black_data[dpy_byte_idx] &= ~(1 << dpy_bit_idx);
                }
            }
        }
    }

    free(image);
    lodepng_state_cleanup(&state);

    return mp_obj_new_tuple(2, (mp_obj_t[]){
                                   mp_obj_new_int(img_w),
                                   mp_obj_new_int(img_h)});
}
// Define a Python reference to the function above.
static MP_DEFINE_CONST_FUN_OBJ_3(decode_png_obj, png_decode_filtered);

// static mp_obj_t python_flush_callback = NULL;
// static mp_buffer_info_t black_framebuf_info;
// static mp_buffer_info_t red_framebuf_info;

typedef struct
{
    mp_obj_t black_framebuf_obj;
    mp_obj_t red_framebuf_obj;
    mp_buffer_info_t black_info;
    mp_buffer_info_t red_info;
} display_state_t;

static display_state_t display_state = {0};

void flush_cb(lv_display_t *display, const lv_area_t *area, uint8_t *px_map)
{
    /* The most simple case (also the slowest) to send all rendered pixels to the
     * screen one-by-one.  `put_px` is just an example.  It needs to be implemented by you. */
    /* Let's say it's a 16 bit (RGB565) display */
    uint16_t *buf16 = (uint16_t *)px_map;
    /* if using a monochrome display (LV_COLOR_DEPTH 1), make sure to skip the palette! (https://lvgl.io/docs/open/master/main-modules/display/color_format.html#monochrome-displays) */
    uint8_t *black_data = display_state.black_info.buf;
    uint8_t *red_data = display_state.red_info.buf;
    int32_t x, y;
    size_t dpy_pxl_idx, dpy_byte_idx, dpy_bit_idx;
    size_t dpy_pxl_w = 960;
    // size_t dpy_pxl_h = 672;
    uint8_t r, g, b;
    uint8_t pxl_black = 0, pxl_red = 0;

    mp_printf(&mp_plat_print, "flush_cb called: %p area (%d,%d)-(%d,%d)\n", 
              display, area->x1, area->y1, area->x2, area->y2);

    for (y = area->y1; y <= area->y2; y++)
    {
        for (x = area->x1; x <= area->x2; x++)
        {
            r = ((*buf16) >> 11) & 0x1F;
            g = ((*buf16) >> 5) & 0x3F;
            b = (*buf16) & 0x1F;
            pxl_red = (r & 0x10) && !(g & 0x20) && !(b & 0x10);
            pxl_black = (!(r & 0x10) && !(g & 0x20) && !(b & 0x10));
            dpy_pxl_idx = y * dpy_pxl_w + x;
            dpy_byte_idx = dpy_pxl_idx / 8;
            dpy_bit_idx = 7 - (dpy_pxl_idx % 8);
            if (pxl_black)
            {
                black_data[dpy_byte_idx] |= (1 << dpy_bit_idx);
            }
            else
            {
                black_data[dpy_byte_idx] &= ~(1 << dpy_bit_idx);
            }
            if (pxl_red)
            {
                red_data[dpy_byte_idx] |= (1 << dpy_bit_idx);
            }
            else
            {
                red_data[dpy_byte_idx] &= ~(1 << dpy_bit_idx);
            }
            red_data[dpy_byte_idx] = 0xff;
            buf16++;
        }
    }

    /* IMPORTANT!!!
     * Inform LVGL that flushing is complete so buffer can be modified again. */
    lv_display_flush_ready(display);
}

// static MP_DEFINE_CONST_FUN_OBJ_3(flush_cb_obj, flush_cb);

static mp_obj_t register_flush_cb(mp_obj_t disp_obj, mp_obj_t framebufs_obj)
{
    lv_display_t *disp_drv = (lv_display_t *)MP_OBJ_TO_PTR(disp_obj);

    // Extract mutable buffers
    size_t framebufs_len = 0;
    mp_obj_t *framebufs;
    mp_obj_tuple_get(framebufs_obj, &framebufs_len, &framebufs);
    if (framebufs_len != 2)
    {
        mp_raise_msg(&mp_type_ValueError, MP_ERROR_TEXT("framebufs must be tuple of 2 framebufs (black, red)"));
    }

    display_state.black_framebuf_obj = framebufs[0];
    display_state.red_framebuf_obj = framebufs[1];
    mp_get_buffer(framebufs[0], &display_state.black_info, MP_BUFFER_WRITE);
    mp_get_buffer(framebufs[1], &display_state.red_info, MP_BUFFER_WRITE);

    lv_display_set_render_mode(disp_drv, LV_DISPLAY_RENDER_MODE_DIRECT);
    lv_display_set_flush_cb(disp_drv, flush_cb);

    mp_printf(&mp_plat_print, "register_flush_cb called %p %p %p\n", &flush_cb, disp_drv->flush_cb, disp_drv);

    // lv_disp_drv_register(&disp_drv);
    return mp_const_none;
}

static MP_DEFINE_CONST_FUN_OBJ_2(register_flush_cb_obj, register_flush_cb);


static mp_obj_t flush_fn(mp_obj_t disp_obj)
{
    mp_printf(&mp_plat_print, "flush_fn called\n");
    lv_display_t *display = (lv_display_t *)MP_OBJ_TO_PTR(disp_obj);
    // lv_layer_t *layer = display->layer_head;
    // while(layer->draw_task_head) {
    //     lv_draw_dispatch_wait_for_request();
    //     lv_draw_dispatch();
    // }

    mp_printf(&mp_plat_print, "flush_fn called %p\n", display);
    // lv_display_flush_ready(display);  // Signal flush is complete
    // mp_printf(&mp_plat_print, "signaled flush ready %p\n", display);
    // lv_refr_now(display);              // Trigger a refresh on this display
    mp_printf(&mp_plat_print, "completed refresh %p\n", display);
    // display->flush_cb(display,  &display->refreshed_area, layer->draw_buf->data);
    return mp_const_none;
}

static MP_DEFINE_CONST_FUN_OBJ_1(flush_fn_obj, flush_fn);

// Define all attributes of the module.
// Table entries are key/value pairs of the attribute name (a string)
// and the MicroPython object reference.
// All identifiers and strings are written as MP_QSTR_xxx and will be
// optimized to word-sized integers by the build system (interned strings).
static const mp_rom_map_elem_t badge_utils_module_globals_table[] = {
    {MP_ROM_QSTR(MP_QSTR___name__), MP_ROM_QSTR(MP_QSTR_badge_utils)},
    {MP_ROM_QSTR(MP_QSTR_decode_png), MP_ROM_PTR(&decode_png_obj)},
    // {MP_ROM_QSTR(MP_QSTR_flush_cb), MP_ROM_PTR(&flush_cb_obj)},
    {MP_ROM_QSTR(MP_QSTR_register_flush_cb), MP_ROM_PTR(&register_flush_cb_obj)},
    {MP_ROM_QSTR(MP_QSTR_flush_fn), MP_ROM_PTR(&flush_fn_obj)},
};
static MP_DEFINE_CONST_DICT(badge_utils_module_globals, badge_utils_module_globals_table);

// Define module object.
const mp_obj_module_t badge_utils_user_cmodule = {
    .base = {&mp_type_module},
    .globals = (mp_obj_dict_t *)&badge_utils_module_globals,
};

// Register the module to make it available in Python.
MP_REGISTER_MODULE(MP_QSTR_badge_utils, badge_utils_user_cmodule);
