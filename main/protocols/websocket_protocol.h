#ifndef _WEBSOCKET_PROTOCOL_H_
#define _WEBSOCKET_PROTOCOL_H_

#include "protocol.h"

#include <freertos/FreeRTOS.h>
#include <freertos/event_groups.h>
#include <web_socket.h>

#define WEBSOCKET_PROTOCOL_SERVER_HELLO_EVENT (1 << 0)

class WebsocketProtocol : public Protocol {
public:
    WebsocketProtocol();
    ~WebsocketProtocol();

    bool Start() override;
    bool SendAudio(std::unique_ptr<AudioStreamPacket> packet) override;
    bool OpenAudioChannel() override;
    void CloseAudioChannel(bool send_goodbye = true) override;
    bool IsAudioChannelOpened() const override;

private:
    mutable std::mutex channel_mutex_;
    EventGroupHandle_t event_group_handle_;
    std::shared_ptr<WebSocket> websocket_;
    int version_ = 1;
    ChannelToken health_channel_token_ = 0;  // Protected by channel_mutex_.
    bool media_started_ = false;
    bool awaiting_hello_ = false;
    bool hello_valid_ = false;

    void ParseServerHello(const cJSON* root, ChannelToken token);
    bool SendText(const std::string& text) override;
    std::string GetHelloMessage();
};

#endif
