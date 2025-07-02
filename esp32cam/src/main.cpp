#include <WiFi.h>
#include <HTTPClient.h>
#include <WebSocketsClient.h>
#include "esp_camera.h"

//                   OLED和时间库
#include <Wire.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>
#include "time.h"

//                       配置与全局变量
const char* WIFI_SSID = "12345678";
const char* WIFI_PASSWORD = "12345678";

// --- 服务器配置 ---
const char* server_host = "192.168.137.1";
const uint16_t server_port = 5000;
const String upload_url = "http://" + String(server_host) + ":" + String(server_port) + "/api/upload";

// --- 硬件引脚定义 ---
#define TRIGGER_BUTTON_PIN 13
#define STATUS_LED_PIN     4

// --- 【新增】OLED屏幕配置 ---
#define SCREEN_WIDTH 128 // OLED display width, in pixels
#define SCREEN_HEIGHT 64 // OLED display height, in pixels
#define OLED_RESET    -1 // Reset pin # (or -1 if sharing Arduino reset pin)
#define I2C_SDA_PIN   15 // 自定义SDA引脚
#define I2C_SCL_PIN   14 // 自定义SCL引脚
Adafruit_SSD1306 display(SCREEN_WIDTH, SCREEN_HEIGHT, &Wire, OLED_RESET);

// --- 【新增】NTP时间配置 ---
const char* ntpServer = "pool.ntp.org";
const long  gmtOffset_sec = 8 * 3600; // GMT+8
const int   daylightOffset_sec = 0;

// --- 【新增】全局状态变量 ---
bool g_isWifiConnected = false;
bool g_isSocketConnected = false;
String g_systemStatus = "Booting...";

// --- 其他全局变量 ---
WebSocketsClient webSocket;
volatile bool shouldTakePhoto = false; 

// --- 函数声明 ---
void initCamera();
void initOLED();
void initTime();
void connectToWiFi();
void setupWebSocket();
void captureAndUpload();
void indicateStatus(int blinks, int duration);
void webSocketEvent(WStype_t type, uint8_t * payload, size_t length);
void updateDisplay();
String getFormattedTime();


void setup() {
    Serial.begin(115200);
    Serial.println("\n\n--- 固件版本 v5.0 (集成OLED显示) 启动 ---");

    initOLED(); // 【新增】初始化OLED

    pinMode(TRIGGER_BUTTON_PIN, INPUT_PULLUP);
    pinMode(STATUS_LED_PIN, OUTPUT);
    digitalWrite(STATUS_LED_PIN, LOW);

    initCamera();
    connectToWiFi();

    if(g_isWifiConnected) {
        initTime(); // 【新增】在Wi-Fi连接后同步时间
    }

    setupWebSocket();
}

void loop() {
    static unsigned long lastDisplayUpdateTime = 0;
    
    webSocket.loop(); 
    
    // 每秒更新一次屏幕显示
    if (millis() - lastDisplayUpdateTime > 1000) {
        lastDisplayUpdateTime = millis();
        updateDisplay();
    }

    if (digitalRead(TRIGGER_BUTTON_PIN) == LOW) {
        g_systemStatus = "Button Pressed";
        updateDisplay();
        Serial.println("物理按钮按下，准备拍照上传...");
        captureAndUpload();
        delay(1000); 
    }
    if (shouldTakePhoto) {
        shouldTakePhoto = false;
        g_systemStatus = "Remote Command";
        updateDisplay();
        Serial.println("接收到远程指令，准备拍照上传...");
        captureAndUpload();
    }
}

//                    OLED 和时间相关函数
void initOLED() {
    // 使用我们自定义的引脚初始化I2C总线
    Wire.begin(I2C_SDA_PIN, I2C_SCL_PIN);

    if(!display.begin(SSD1306_SWITCHCAPVCC, 0x3C)) { 
        Serial.println(F("SSD1306 allocation failed"));
        for(;;); // Don't proceed, loop forever
    }
    display.clearDisplay();
    display.setTextSize(1);
    display.setTextColor(SSD1306_WHITE);
    display.setCursor(0, 0);
    display.println("System Booting...");
    display.display();
}

void initTime() {
    g_systemStatus = "Syncing Time...";
    updateDisplay();
    configTime(gmtOffset_sec, daylightOffset_sec, ntpServer);
    g_systemStatus = "Time Synced!";
    updateDisplay();
    delay(1000);
}

String getFormattedTime() {
    struct tm timeinfo;
    if(!getLocalTime(&timeinfo)){
        return "Time not set";
    }
    char timeString[9];
    strftime(timeString, sizeof(timeString), "%H:%M:%S", &timeinfo);
    return String(timeString);
}

void updateDisplay() {
    display.clearDisplay();
    display.setTextSize(1);
    display.setTextColor(SSD1306_WHITE);
    
    // 第一行：显示时间
    display.setCursor(0, 0);
    display.print("Time: ");
    display.println(getFormattedTime());

    // 第二行：Wi-Fi状态
    display.setCursor(0, 10);
    display.print("Wi-Fi: ");
    display.println(g_isWifiConnected ? "Connected" : "Offline");

    // 第三行：服务器(WebSocket)状态
    display.setCursor(0, 20);
    display.print("Server: ");
    display.println(g_isSocketConnected ? "Connected" : "Offline");
    
    // 第四行：当前系统状态
    display.setCursor(0, 30);
    display.print("Status: ");
    display.println(g_systemStatus);

    display.display();
}


//                    核心功能函数
void webSocketEvent(WStype_t type, uint8_t * payload, size_t length) {
    switch(type) {
        case WStype_DISCONNECTED: 
            Serial.println("[WebSocket] 已断开连接!"); 
            g_isSocketConnected = false;
            g_systemStatus = "Server Offline";
            break;
        case WStype_ERROR: 
            Serial.printf("[WebSocket] 发生错误: %s\n", payload); 
            g_isSocketConnected = false;
            g_systemStatus = "Socket Error";
            break;
        case WStype_CONNECTED:
            Serial.printf("[WebSocket] 已连接到: %s\n", payload);
            webSocket.sendTXT("40");
            break;
        case WStype_TEXT:
            if (length > 0) {
                if (payload[0] == '2') { 
                    webSocket.sendTXT("3"); 
                }
                else if (payload[0] == '4' && payload[1] == '0') {
                    Serial.println("Socket.IO 握手成功, 发送硬件报到...");
                    g_isSocketConnected = true;
                    g_systemStatus = "Ready";
                    webSocket.sendTXT("42[\"hardware_hello\",{}]");
                }
                else if (strstr((char *)payload, "take_photo")) {
                    Serial.println("解析到远程拍照指令!");
                    shouldTakePhoto = true;
                }
            }
            break;
        default: 
            break;
    }
}

void connectToWiFi() {
    g_systemStatus = "Connecting WiFi...";
    updateDisplay();
    Serial.print("正在连接Wi-Fi: ");
    Serial.println(WIFI_SSID);
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
    unsigned long startTime = millis();
    while (WiFi.status() != WL_CONNECTED) {
        delay(500);
        Serial.print(".");
        if (millis() - startTime > 15000) { 
            Serial.println("\nWi-Fi连接超时。");
            g_isWifiConnected = false;
            g_systemStatus = "WiFi Failed";
            return;
        }
    }
    Serial.println("\nWi-Fi连接成功!");
    Serial.print("IP地址: ");
    Serial.println(WiFi.localIP());
    g_isWifiConnected = true;
    g_systemStatus = "WiFi Connected";
}

void setupWebSocket() {
    g_systemStatus = "Connecting Server";
    updateDisplay();
    Serial.println("正在设置 WebSocket...");
    webSocket.begin(server_host, server_port, "/socket.io/?EIO=4&transport=websocket");
    webSocket.onEvent(webSocketEvent);
    webSocket.setReconnectInterval(5000);
}

void captureAndUpload() {
    g_systemStatus = "Taking Photo...";
    updateDisplay();
    indicateStatus(1, 50);
    camera_fb_t * fb = esp_camera_fb_get();
    if (!fb) {
        Serial.println("拍照失败");
        g_systemStatus = "Photo Failed";
        indicateStatus(3, 300);
        return;
    }
    
    g_systemStatus = "Uploading...";
    updateDisplay();
    HTTPClient http;
    http.begin(upload_url);
    Serial.println("正在上传图片...");
    int httpResponseCode = http.POST(fb->buf, fb->len);
    if (httpResponseCode > 0) {
        Serial.printf("上传成功, HTTP代码: %d\n", httpResponseCode);
        g_systemStatus = "Upload OK";
    } else {
        Serial.printf("上传失败, 错误: %s\n", http.errorToString(httpResponseCode).c_str());
        g_systemStatus = "Upload Fail";
    }

    http.end();
    esp_camera_fb_return(fb);
    updateDisplay(); 
    delay(2000); 
    g_systemStatus = "Ready"; 
}

void initCamera() {
    camera_config_t config;
    config.pin_pwdn = 32;
    config.pin_reset = -1;
    config.pin_xclk = 0;
    config.pin_sccb_sda = 26;
    config.pin_sccb_scl = 27;
    config.pin_d7 = 35;
    config.pin_d6 = 34;
    config.pin_d5 = 39;
    config.pin_d4 = 36;
    config.pin_d3 = 21;
    config.pin_d2 = 19;
    config.pin_d1 = 18;
    config.pin_d0 = 5;
    config.pin_vsync = 25;
    config.pin_href = 23;
    config.pin_pclk = 22;
    config.ledc_channel = LEDC_CHANNEL_0;
    config.ledc_timer = LEDC_TIMER_0;
    config.xclk_freq_hz = 20000000;
    config.pixel_format = PIXFORMAT_JPEG;
    config.frame_size = FRAMESIZE_UXGA;
    config.jpeg_quality = 12;
    config.fb_count = 1;
    config.grab_mode = CAMERA_GRAB_WHEN_EMPTY;
    config.fb_location = CAMERA_FB_IN_PSRAM;
    
    if(psramFound()){
      config.jpeg_quality = 10;
      config.fb_count = 2;
      config.grab_mode = CAMERA_GRAB_LATEST;
      Serial.println("PSRAM检测成功，使用高质量配置。");
    } else {
      config.frame_size = FRAMESIZE_SVGA;
      config.fb_location = CAMERA_FB_IN_DRAM;
      Serial.println("警告: 未检测到PSRAM，使用低质量备用配置。");
    }
    
    esp_err_t err = esp_camera_init(&config);
    if (err != ESP_OK) {
        Serial.printf("摄像头初始化失败，错误码: 0x%x\n", err);
        g_systemStatus = "Cam Init Fail";
        updateDisplay();
        return; 
    }
    Serial.println("摄像头初始化成功。");
    g_systemStatus = "Camera OK";
    updateDisplay();
    
    sensor_t * s = esp_camera_sensor_get();
    s->set_vflip(s, 1);
    s->set_brightness(s, 1);
    s->set_saturation(s, -2);
}

void indicateStatus(int blinks, int duration) {
    for (int i = 0; i < blinks; i++) {
        digitalWrite(STATUS_LED_PIN, HIGH);
        delay(duration);
        digitalWrite(STATUS_LED_PIN, LOW);
        if (i < blinks - 1) delay(duration);
    }
}