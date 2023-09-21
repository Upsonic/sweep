import json
from datetime import datetime, timedelta
from typing import Any

import requests
from geopy import Nominatim, exc
from logn import logger
from pydantic import BaseModel, Field
from pymongo import MongoClient, errors

from sweepai.config.server import (
    MONGODB_URI,
    DISCORD_WEBHOOK_URL,
    SUPPORT_COUNTRY,
    DISCORD_LOW_PRIORITY_URL,
    DISCORD_MEDIUM_PRIORITY_URL,
)


class ChatLogger(BaseModel):
    data: dict
    chat_collection: Any = None
    ticket_collection: Any = None
    expiration: datetime = None
    index: int = 0
    current_date: str = Field(
        default_factory=lambda: datetime.utcnow().strftime("%m/%Y/%d")
    )
    current_month: str = Field(
        default_factory=lambda: datetime.utcnow().strftime("%m/%Y")
    )

    def __init__(self, data: dict):
        super().__init__(data=data)  # Call the BaseModel's __init__ method
        self.initialize_mongodb()
    
    def initialize_mongodb(self):
        key = MONGODB_URI
        if key is None:
            logger.warning("Chat history logger has no key")
            return
        try:
            client = MongoClient(
                key, serverSelectionTimeoutMS=5000, socketTimeoutMS=5000
            )
            db = client["llm"]
            self.chat_collection = db["chat_history"]
            self.ticket_collection = db["tickets"]
            self.create_indexes()
            self.set_expiration()
        except errors.ServerSelectionTimeoutError as e:
            logger.warning("Chat history could not connect to MongoDB due to server selection timeout")
            logger.warning(e)
        except errors.ConnectionFailure as e:
            logger.warning("Chat history could not connect to MongoDB due to connection failure")
            logger.warning(e)
        except SystemExit:
            raise SystemExit
        except Exception as e:
            logger.warning("Chat history could not connect to MongoDB due to an unknown error")
            logger.warning(e)
    
    def create_indexes(self):
        self.ticket_collection.create_index("username")
        self.chat_collection.create_index(
            "expiration", expireAfterSeconds=2419200
        )  # 28 days data persistence
    
    def set_expiration(self):
        self.expiration = datetime.utcnow() + timedelta(
            days=1
        )  # 1 day since historical use case

    def get_chat_history(self, filters):
        return (
            self.chat_collection.find(filters)
            .sort([("expiration", 1), ("index", 1)])
            .limit(2000)
        )

    def add_chat(self, additional_data):
        if self.chat_collection is None:
            logger.error("Chat collection is not initialized")
            return
        document = {
            **self.data,
            **additional_data,
            "expiration": self.expiration,
            "index": self.index,
        }
        self.index += 1
        self.chat_collection.insert_one(document)

    def add_successful_ticket(self, gpt3=False):
        if self.ticket_collection is None:
            logger.error("Ticket Collection Does Not Exist")
            return
        username = self.data["username"]
        if "assignee" in self.data:
            username = self.data["assignee"]
        if gpt3:
            key = f"{self.current_month}_gpt3"
            self.ticket_collection.update_one(
                {"username": username},
                {"$inc": {key: 1}},
                upsert=True,
            )
        else:
            self.ticket_collection.update_one(
                {"username": username},
                {"$inc": {self.current_month: 1, self.current_date: 1}},
                upsert=True,
            )
        logger.info(f"Added Successful Ticket for {username}")

    def get_ticket_count(self, use_date=False, gpt3=False):
        # gpt3 overrides use_date
        if self.ticket_collection is None:
            logger.error("Ticket Collection Does Not Exist")
            return 0
        username = self.data["username"]
        tracking_date = self.current_date if use_date else self.current_month
        if gpt3:
            tracking_date = f"{self.current_month}_gpt3"
        result = self.ticket_collection.aggregate(
            [
                {"$match": {"username": username}},
                {"$project": {tracking_date: 1, "_id": 0}},
            ]
        )
        result_list = list(result)
        ticket_count = (
            result_list[0].get(tracking_date, 0) if len(result_list) > 0 else 0
        )
        logger.info(f"Ticket Count for {username} {ticket_count}")
        return ticket_count

    def is_paying_user(self):
        if self.ticket_collection is None:
            logger.error("Ticket Collection Does Not Exist")
            return False
        username = self.data["username"]
        result = self.ticket_collection.find_one({"username": username})
        return result.get("is_paying_user", False) if result else False

    def is_trial_user(self):
        if self.ticket_collection is None:
            logger.error("Ticket Collection Does Not Exist")
            return False
        username = self.data["username"]
        result = self.ticket_collection.find_one({"username": username})
        return result.get("is_trial_user", False) if result else False

    def use_faster_model(self, g):
        if self.ticket_collection is None:
            logger.error("Ticket Collection Does Not Exist")
            return True
    
        use_faster = False
        if self.is_paying_user():
            use_faster = self.get_ticket_count() >= 500
        elif self.is_trial_user():
            use_faster = self.get_ticket_count() >= 15
        else:
            use_faster = self.check_location_and_ticket_count(g)
    
        return use_faster
    
    def check_location_and_ticket_count(self, g):
        try:
            loc_user = g.get_user(self.data["username"]).location
            loc = Nominatim(user_agent="location_checker").geocode(
                loc_user, exactly_one=True
            )
            if not self.is_supported_country(loc):
                logger.print("G EXCEPTION", loc_user)
                return (
                    self.get_ticket_count() >= 5
                    or self.get_ticket_count(use_date=True) >= 1
                )
        except exc.GeocoderTimedOut as e:
            logger.warning("Geolocation service timed out")
            logger.warning(e)
        except exc.GeocoderServiceError as e:
            logger.warning("Geolocation service error occurred")
            logger.warning(e)
        except SystemExit:
            raise SystemExit
        except Exception as e:
            logger.warning("An unknown error occurred during geolocation")
            logger.warning(e)
    
        # Non-trial users can only create 2 GPT-4 tickets per day
        return self.get_ticket_count() >= 5 or self.get_ticket_count(use_date=True) > 3
    
    def is_supported_country(self, loc):
        for c in SUPPORT_COUNTRY:
            if c.lower() in loc.raw.get("display_name").lower():
                return True
        return False


def discord_log_error(content, priority=0):
    """
    priority: 0 (high), 1 (medium), 2 (low)
    """
    try:
        url = DISCORD_WEBHOOK_URL
        if priority == 1:
            url = DISCORD_MEDIUM_PRIORITY_URL
        if priority == 2:
            url = DISCORD_LOW_PRIORITY_URL

        data = {"content": content}
        headers = {"Content-Type": "application/json"}
        response = requests.post(url, data=json.dumps(data), headers=headers)
        if response.status_code != 204:
            logger.error(f"Failed to log to Discord: {response.status_code}")
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to send HTTP request to Discord")
        logger.error(e)
    except SystemExit:
        raise SystemExit
    except Exception as e:
        logger.error(f"An unknown error occurred while logging to Discord")
        logger.error(e)
