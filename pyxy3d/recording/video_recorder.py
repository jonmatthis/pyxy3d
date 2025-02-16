import pyxy3d.logger

logger = pyxy3d.logger.get(__name__)

# from PyQt6.QtCore import QObject, pyqtSignal
from pathlib import Path
from queue import Queue
from threading import Thread, Event
import cv2
import sys
import pandas as pd
import shutil

from pyxy3d.cameras.synchronizer import Synchronizer
from pyxy3d.cameras.live_stream import LiveStream
from pyxy3d.interface import FramePacket, SyncPacket


class VideoRecorder:

    def __init__(self, synchronizer: Synchronizer, suffix: str = None):
        super().__init__()
        self.synchronizer = synchronizer

        # set text to be appended as port_X_{suffix}.mp4
        # will also be appended to xy_{suffix}
        if suffix is not None:
            self.suffix = "_" + suffix
        else:
            self.suffix = ""

        self.recording = False
        self.sync_index = 0  # no sync packets at init... absence of initialized value can cause errors elsewhere
        # build dict that will be stored to csv
        self.trigger_stop = Event()

        self.sync_packet_in_q = Queue(-1)

    def build_video_writers(self):
        """
        suffix provides a way to provide additional labels to the mp4 file name
        This would be relevant when performing post-processing and saving out frames with points
        """
        # create a dictionary of videowriters
        self.video_writers = {}
        for port, stream in self.synchronizer.streams.items():
            path = str(Path(self.destination_folder, f"port_{port}{self.suffix}.mp4"))
            logger.info(f"Building video writer for port {port}; recording to {path}")
            fourcc = cv2.VideoWriter_fourcc(*"MP4V")
            frame_size = stream.camera.size

            writer = cv2.VideoWriter(path, fourcc, stream.fps, frame_size)
            self.video_writers[port] = writer

    def save_data_worker(self, include_video: bool, show_points: bool, store_point_history:bool):
        # connect video recorder to synchronizer via an "in" queue
        if include_video:
            self.build_video_writers()

        # I think I put this here so that it will get reset if you reuse the same recorder..
        self.frame_history = {
            "sync_index": [],
            "port": [],
            "frame_index": [],
            "frame_time": [],
        }

        self.point_data_history = {
            "sync_index": [],
            "port": [],
            "frame_index": [],
            "frame_time": [],
            "point_id": [],
            "img_loc_x": [],
            "img_loc_y": [],
            "obj_loc_x": [],
            "obj_loc_y": [],
        }

        
        self.synchronizer.subscribe_to_sync_packets(self.sync_packet_in_q)
        syncronizer_subscription_released = False
        
        # this is where the issue is... need to figure out when the queue is empty...
        logger.info("Entering Save data worker loop entered")
        while self.sync_packet_in_q.qsize() > 0 or not self.trigger_stop.is_set(): 
            sync_packet: SyncPacket = self.sync_packet_in_q.get()

            # provide periodic updates of recording queue
            logger.debug("Getting size of sync packet q")
            backlog = self.sync_packet_in_q.qsize()
            if backlog % 25 == 0 and backlog !=0:
                logger.info(f"Size of unsaved frames on the recording queue is {self.sync_packet_in_q.qsize()}")

            if sync_packet is None:
                # relenvant when 
                logger.info("End of sync packets signaled...breaking record loop")
                break

            self.sync_index = sync_packet.sync_index

            for port, frame_packet in sync_packet.frame_packets.items():
                if frame_packet is not None:
                    logger.debug("Processiong frame packet...")
                    # read in the data for this frame for this port
                    if show_points:
                        frame = frame_packet.frame_with_points
                    else:
                        frame = frame_packet.frame

                    frame_index = frame_packet.frame_index
                    frame_time = frame_packet.frame_time

                    if include_video:
                        # store the frame
                        logger.debug("Writing frame")
                        self.video_writers[port].write(frame)

                        # store to assocated data in the dictionary
                        self.frame_history["sync_index"].append(self.sync_index)
                        self.frame_history["port"].append(port)
                        self.frame_history["frame_index"].append(frame_index)
                        self.frame_history["frame_time"].append(frame_time)

                    new_tidy_table = frame_packet.to_tidy_table(self.sync_index)
                    if new_tidy_table is not None:  # i.e. it has data
                        for key, value in self.point_data_history.copy().items():
                            logger.debug("Extending tidy table of point history")
                            self.point_data_history[key].extend(new_tidy_table[key])
                        
            if not syncronizer_subscription_released and self.trigger_stop.is_set():
                logger.info("Save frame worker winding down...")
                syncronizer_subscription_released = True
                self.synchronizer.release_sync_packet_q(self.sync_packet_in_q)
                # self.sync_packet_in_q = Queue(-1)
                # self.recording_stop_signal.emit()

        # a proper release is strictly necessary to ensure file is readable
        if include_video:
            logger.info("releasing video writers...")
            for port in self.synchronizer.ports:
                self.video_writers[port].release()

            # del self.video_writers

            logger.info("Initiate storing of frame history")
            self.store_frame_history()

        logger.info("Initiate storing of point history")
        if store_point_history:
            self.store_point_history()
        self.trigger_stop.clear()  # reset stop recording trigger
        self.recording = False
        logger.info("About to emit `all frames saved` signal")
        # self.all_frames_saved_signal.emit()


    def store_point_history(self):
        df = pd.DataFrame(self.point_data_history)
        point_data_path = str(Path(self.destination_folder, f"xy{self.suffix}.csv"))
        logger.info(f"Storing point data in {point_data_path}")
        df.to_csv(point_data_path, index=False, header=True)

    def store_frame_history(self):
        df = pd.DataFrame(self.frame_history)
        frame_hist_path = str(Path(self.destination_folder, "frame_time_history.csv"))
        logger.info(f"Storing frame history to {frame_hist_path}")
        df.to_csv(frame_hist_path, index=False, header=True)
    
    def store_active_config(self):
        pass

    def start_recording(
        self,
        destination_folder: Path,
        include_video=True,
        show_points=False,
        store_point_history=True,
    ):
        """
        Option exists to not store video if only interested in getting points from original video
        
        Parent of destination folder will be the source of the config file that will be stored with the video
        This enables the nested processing of videos (i.e. Recording_1 will store the main config.toml,
        then POSE subfolder will store config.toml from Recording_1). Each folder should largely become self
        contained and portable for analysis / reconstruction.
        """
        logger.info(f"All video data to be saved to {destination_folder}")

        self.destination_folder = destination_folder
        # create the folder if it doesn't already exist
        self.destination_folder.mkdir(exist_ok=True, parents=True)
       
        # Because calibration files are nested in a calibration directory, need to go 
        # to parent.parent to get the config.toml file      
        if self.destination_folder.parent.stem == "calibration":
            source_config_path = Path(self.destination_folder.parent.parent, "config.toml")
        else:   # just a regular recording
            source_config_path = Path(self.destination_folder.parent, "config.toml")

        duplicate_config_path = Path(self.destination_folder,"config.toml")
        
        shutil.copy2(source_config_path,duplicate_config_path)

        self.recording = True
        self.recording_thread = Thread(
            target=self.save_data_worker, args=[include_video, show_points, store_point_history], daemon=True
        )
        self.recording_thread.start()

    def stop_recording(self):
        logger.info("about to Stop recording initiated within VideoRecorder")
        self.trigger_stop.set()
        logger.info("Stop recording initiated within VideoRecorder")
