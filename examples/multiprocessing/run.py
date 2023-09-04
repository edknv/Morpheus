# SPDX-FileCopyrightText: Copyright (c) 2023, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import cupy
import ctypes
import multiprocessing as mp
import os
import threading as mt
import time
from multiprocessing.connection import Connection

import mrc
from mrc.core.subscriber import Observer

import cudf

from morpheus.config import Config
from morpheus.messages.multi_message import MultiMessage
from morpheus.pipeline.linear_pipeline import LinearPipeline
from morpheus.pipeline.single_port_stage import SinglePortStage
from morpheus.pipeline.stream_pair import StreamPair
from morpheus.stages.input.in_memory_source_stage import InMemorySourceStage
from morpheus.stages.output.in_memory_sink_stage import InMemorySinkStage
from morpheus.stages.postprocess.serialize_stage import SerializeStage
from morpheus.stages.preprocess.deserialize_stage import DeserializeStage


class MyMultiprocessingStage(SinglePortStage):

    def __init__(self, config: Config):
        super().__init__(config)
        # must use spawn because we can't fork the cuda context.
        self.mp_context = mp.get_context("spawn")  
        self.cancellation_token = self.mp_context.Value(ctypes.c_int8, False)
    @property
    def name(self) -> str:
        return "my-multiprocessing"

    def accepted_types(self) -> tuple:
        return (MultiMessage, )

    def supports_cpp_node(self):
        return False

    @staticmethod
    def child_receive(queue_recv: mp.Queue, queue_send: mp.Queue, cancellation_token: mp.Value):
        print("===== Started child receive =====", os.getppid(), os.getpid())
        while not cancellation_token.value:
            if queue_recv.qsize() == 0:
                print("child: waiting...")
                time.sleep(1)
                continue

            event = queue_recv.get()

            if event["type"] == "on_next":

                #  we can do our fancy processing here.

                message: MultiMessage = event["value"]
                message.set_meta("b", cupy.arange(message.mess_count))
                queue_send.put({ "type": "on_next", "value": message })
                continue

            if event["type"] == "on_completed":
                queue_send.put(event)
                break

            if event["type"] == "on_error":
                queue_send.put(event)
                break

        print("child: closing...")

    @staticmethod
    def parent_receive(queue_recv: mp.Queue, sub: mrc.Subscriber, cancellation_token: mp.Value):
            print("===== Started parent receive =====", os.getpid())
            while not cancellation_token.value:
                if queue_recv.qsize() == 0:
                    print("parent: waiting...")
                    time.sleep(1)
                    continue

                event = queue_recv.get()

                if event["type"] == "on_next":
                    sub.on_next(event["value"])
                    continue

                if event["type"] == "on_error":
                    sub.on_next(event["value"])
                    break

                if event["type"] == "on_completed":
                    sub.on_completed()
                    break

    def generate(self, obs: mrc.Observable, sub: mrc.Subscriber):

        # generate is called on subscribe

        print("parent: closing...")

        pre_queue = self.mp_context.Queue()
        post_queue = self.mp_context.Queue()
        my_process = self.mp_context.Process(target=MyMultiprocessingStage.child_receive, args=(pre_queue, post_queue, self.cancellation_token))
        my_thread = mt.Thread(target=MyMultiprocessingStage.parent_receive, args=(post_queue, sub, self.cancellation_token))

        def on_next(message: MultiMessage):
            print("obs on next", os.getpid())
            pre_queue.put({"type": "on_next", "value": message})

        def on_error(error: BaseException):
            print("obs on error", os.getpid())
            pre_queue.put({"type": "on_error", "value": error})

        def on_completed():
            print("obs on completed", os.getpid())
            pre_queue.put({"type": "on_completed"})

        my_process.start()
        my_thread.start()

        # forward the subscribe call

        obs.subscribe(Observer.make_observer(on_next, on_error, on_completed))

        my_thread.join()
        my_process.join()

    def _build_single(self, builder: mrc.Builder, input_stream: StreamPair):
        stream = builder.make_node("my-multiprocessing", mrc.core.operators.build(self.generate))
        builder.make_edge(input_stream[0], stream)
        return stream, input_stream[1]
    
    def stop(self):
        super().stop()
        self.cancellation_token.value = True


def run_pipeline():

    config = Config()

    df_input = cudf.DataFrame({"name": ["five","four","three","two","one"], "value": [5,4,3,2,1]})

    pipeline = LinearPipeline(config)
    pipeline.set_source(InMemorySourceStage(config, [df_input]))
    pipeline.add_stage(DeserializeStage(config))
    pipeline.add_stage(MyMultiprocessingStage(config))
    pipeline.add_stage(SerializeStage(config))
    sink = pipeline.add_stage(InMemorySinkStage(config))

    pipeline.run()

    messages = sink.get_messages()

    if len(messages) > 0:
        print(messages[0].copy_dataframe())


if __name__ == "__main__":
    run_pipeline()