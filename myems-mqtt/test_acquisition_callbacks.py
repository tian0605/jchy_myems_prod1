import logging
import unittest
from unittest.mock import patch

import paho.mqtt.client as mqtt

import acquisition


class AcquisitionCallbackTests(unittest.TestCase):
    def setUp(self):
        clients = []
        real_client = mqtt.Client
        self.logger = logging.getLogger(self.id())

        def create_client(*args, **kwargs):
            client = real_client(*args, **kwargs)
            clients.append(client)
            return client

        with patch.object(acquisition, '_update_process_id'), \
             patch.object(acquisition, '_load_points', return_value={
                 1: {'point_id': 1, 'object_type': 'ENERGY_VALUE', 'is_trend': True},
             }), \
             patch.object(acquisition.mqtt, 'Client', side_effect=create_client), \
             patch.object(real_client, 'connect_async'), \
             patch.object(real_client, 'loop_start'), \
             patch.object(real_client, 'loop_stop'), \
             patch.object(real_client, 'disconnect'), \
             patch.object(acquisition.time, 'sleep', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                acquisition.process(self.logger, 1, {
                    'host': 'unused', 'port': 1883, 'topic': 'test', 'qos': 1,
                })
        self.client, = clients
        self.addCleanup(self.client.disconnect)
        self.assertEqual(self.client.callback_api_version, mqtt.CallbackAPIVersion.VERSION2)

    def dispatch_connack(self, packet):
        # Paho's dispatcher reads its decoded packet buffer rather than an argument.
        self.client._in_packet['remaining_length'] = len(packet)
        self.client._in_packet['packet'] = bytearray(packet)
        return self.client._handle_connack()

    def test_successful_connection_subscribes(self):
        with patch.object(self.client, 'subscribe') as subscribe:
            result = self.dispatch_connack(b'\x00\x00\x00')
        self.assertEqual(result, mqtt.MQTT_ERR_SUCCESS)
        subscribe.assert_called_once_with('test', qos=1)

    def test_rejected_connection_logs_without_subscribing(self):
        with patch.object(self.client, 'subscribe') as subscribe, \
             self.assertLogs(self.logger, level='ERROR') as logs:
            self.dispatch_connack(b'\x00\x87\x00')
        subscribe.assert_not_called()
        self.assertIn('Not authorized', logs.output[0])

    def test_disconnect_dispatch_accepts_version2_arguments(self):
        with self.assertLogs(self.logger, level='WARNING') as logs:
            self.client._do_on_disconnect(
                packet_from_broker=False, v1_rc=mqtt.MQTT_ERR_CONN_LOST,
            )
        self.assertIn('MQTT client disconnected for data source 1', logs.output[0])


if __name__ == '__main__':
    unittest.main()
