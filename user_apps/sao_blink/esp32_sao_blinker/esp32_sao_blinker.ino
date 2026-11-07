// ESP32 SAO "blinker": runs on its own ESP32 (C3/S3/plain), powered from the
// badge's SAO header, and listens for commands from the badge.
//
//  - I2C target (slave) at 0x42. Badge writes 1 byte = mode:
//        0 off, 1 on, 2 slow blink, 3 fast blink
//    Badge reads 1 byte = current mode.
//  - TRIGGER_PIN is wired to the SAO's GPIO1 pin. Each rising edge from the
//    badge advances the mode too (no I2C needed).
//
// Arduino IDE: install "esp32 by Espressif Systems" (core 3.x), pick your board,
// flash over the ESP32's own USB *while it is NOT plugged into the badge*.
//
// SAO header (badge side, 2x3, 2.54 mm):  3V3  GND  SDA  SCL  GPIO1  GPIO2

#include <Wire.h>

// ---- change these for your board ----
#define I2C_ADDR     0x42
#define SDA_PIN      6      // to SAO SDA  (badge GPIO17)
#define SCL_PIN      7      // to SAO SCL  (badge GPIO18)
#define TRIGGER_PIN  5      // to SAO GPIO1 (badge GPIO2 / 4 / 7 depending on header)
#define LED_PIN      8      // ESP32-C3 SuperMini onboard LED; or any LED + 330R
#define LED_ACTIVE_LOW true // SuperMini LED is active-low
// --------------------------------------

volatile uint8_t mode = 2;   // start in slow blink so you can see it's alive

void onReceive(int len) {
  while (Wire.available()) mode = Wire.read() % 4;
}

void onRequest() {
  Wire.write(mode);
}

void IRAM_ATTR onTrigger() {
  mode = (mode + 1) % 4;
}

void setLed(bool on) {
  digitalWrite(LED_PIN, (on ^ LED_ACTIVE_LOW) ? HIGH : LOW);
}

void setup() {
  pinMode(LED_PIN, OUTPUT);
  pinMode(TRIGGER_PIN, INPUT_PULLDOWN);
  attachInterrupt(digitalPinToInterrupt(TRIGGER_PIN), onTrigger, RISING);

  Wire.onReceive(onReceive);
  Wire.onRequest(onRequest);
  Wire.begin((uint8_t)I2C_ADDR, SDA_PIN, SCL_PIN, 100000);

  // No WiFi/BLE: keeps current draw low (~20-40 mA) so the badge's 3.3 V rail is happy.
}

void loop() {
  uint32_t t = millis();
  switch (mode) {
    case 0: setLed(false); break;
    case 1: setLed(true); break;
    case 2: setLed((t / 500) % 2); break;
    case 3: setLed((t / 100) % 2); break;
  }
  delay(5);
}
