from typing import List
from pydantic import BaseModel


class Response(BaseModel):
    message:str



class ServicesCheckOut(BaseModel):
    id:str
    slug:str
    url:str

class Items(BaseModel):
    items:List[ServicesCheckOut]