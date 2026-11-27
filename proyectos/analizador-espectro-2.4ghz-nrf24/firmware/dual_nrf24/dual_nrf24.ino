// Two nRF24L01+ radios on one ESP32-WROOM-32E sharing the VSPI bus.
//
// Wiring (both modules: VCC=3V3, GND=GND, add 10uF cap across VCC/GND):
//   Signal | Radio A | Radio B
//   SCK    | GPIO18  | GPIO18
//   MISO   | GPIO19  | GPIO19
//   MOSI   | GPIO23  | GPIO23
//   CSN    | GPIO5   | GPIO17
//   CE     | GPIO4   | GPIO16
//
// Self-test: radio A transmits a counter, radio B receives it.
#include <SPI.h>   // SPI bus driver (ships with the ESP32 core)
#include "RF24.h"  // TMRh20 RF24 library for the nRF24L01+

constexpr uint8_t RADIO_A_CE = 4;
constexpr uint8_t RADIO_A_CSN = 5;
constexpr uint8_t RADIO_B_CE = 16;
constexpr uint8_t RADIO_B_CSN = 17;

constexpr uint8_t CHANNEL = 76;
const uint8_t PIPE_ADDRESS[6] = "NODE1";

RF24 radioA(RADIO_A_CE, RADIO_A_CSN);
RF24 radioB(RADIO_B_CE, RADIO_B_CSN);

uint32_t counter = 0;

static bool initRadio(RF24 &radio, const char *name) {
  if (!radio.begin(&SPI)) {
    Serial.printf("[%s] begin() failed: check wiring/power\n", name);
    return false;
  }
  if (!radio.isChipConnected()) {
    Serial.printf("[%s] chip not responding on SPI\n", name);
    return false;
  }
  radio.setChannel(CHANNEL);
  radio.setPALevel(RF24_PA_LOW);  // radios are close together
  radio.setDataRate(RF24_1MBPS);
  radio.setPayloadSize(sizeof(counter));
  Serial.printf("[%s] OK\n", name);
  return true;
}

void setup() {
  Serial.begin(115200);
  delay(500);
  Serial.println("\nESP32 dual nRF24L01+ test");

  // Keep both CSN lines high before touching the shared bus.
  pinMode(RADIO_A_CSN, OUTPUT);
  pinMode(RADIO_B_CSN, OUTPUT);
  digitalWrite(RADIO_A_CSN, HIGH);
  digitalWrite(RADIO_B_CSN, HIGH);

  SPI.begin();  // VSPI defaults: SCK=18, MISO=19, MOSI=23

  bool okA = initRadio(radioA, "A");
  bool okB = initRadio(radioB, "B");
  if (!okA || !okB) {
    Serial.println("Halting: fix the failing radio and reset.");
    while (true) delay(1000);
  }

  radioA.openWritingPipe(PIPE_ADDRESS);
  radioA.stopListening();

  radioB.openReadingPipe(1, PIPE_ADDRESS);
  radioB.startListening();
}

void loop() {
  bool acked = radioA.write(&counter, sizeof(counter));
  Serial.printf("A -> sent %lu %s\n", (unsigned long)counter, acked ? "(ACK)" : "(no ACK)");

  uint32_t start = millis();
  while (!radioB.available() && millis() - start < 50) {
  }
  if (radioB.available()) {
    uint32_t received = 0;
    radioB.read(&received, sizeof(received));
    Serial.printf("B <- got  %lu\n", (unsigned long)received);
  } else {
    Serial.println("B <- nothing received");
  }

  counter++;
  delay(1000);
}
