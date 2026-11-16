# SPDX-License-Identifier: GPL-2.0-or-later
include $(BR2_EXTERNAL_LAPINLIBRE_PATH)/package/karotz-era.mk.inc
include $(sort $(wildcard $(BR2_EXTERNAL_LAPINLIBRE_PATH)/package/*/*.mk))
# paho-mqtt-c without TLS: Buildroot builds it with TLS (libpaho-mqtt3as) whenever OpenSSL is in
# the build, as it is here; CMake takes the last -D given. lapinou-mqtt links libpaho-mqtt3a.
PAHO_MQTT_C_CONF_OPTS += -DPAHO_WITH_SSL=FALSE
