from typing import Union
from fastapi import FastAPI, Depends
from schemas import Item, Phone, Question
from sqlalchemy.orm import Session
from database import db
from models import Base
import models
import os
import uuid
from fastapi import FastAPI, File
from fastapi.responses import FileResponse
from vxml_builder import QuestionBuilder, HomeBuilder, PhoneBuilder
import urllib.parse
from tts import ICT4DTTS

from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()

origins = ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

ENV_VAR_DB_URL = "DATABASE_URL"
HEROKU_URL = "HEROKU_URL"
OPENAI_KEY = "OPENAI_API"

db_url = os.getenv(ENV_VAR_DB_URL, "localhost")
heroku_url = os.getenv(HEROKU_URL, "localhost")
openai_api = os.getenv(OPENAI_KEY, "OPENAI")
db_url = db_url.replace('postgres', 'postgresql')

psql = db(db_url)


@app.get("/")
def read_root():
    return {"Hello": "World"}


@app.get("/items/{item_id}")
def read_item(item_id: int, q: Union[str, None] = None):
    return {"item_id": item_id, "q": q}


@app.post("/api/new_phone/{phone}")
def add_new_phone(phone: str, db: Session = Depends(psql.connect)):
    new_phone = models.PhonePool(phone=phone)
    db.add(new_phone)

    db.commit()
    db.refresh(new_phone)

    phonevxml = PhoneBuilder(phone, heroku_url)
    phonevxml.commit()

    return new_phone


@app.get("/api/all_phones")
def all_phones(db: Session = Depends(psql.connect)):
    return db.query(models.PhonePool).all()


@app.get("/api/free_phones")
def free_phones(db: Session = Depends(psql.connect)):
    return db.query(models.PhonePool).filter(
        models.PhonePool.question_type == None).all()


@app.get("/api/vote/{number}")
def vote(number: str, db: Session = Depends(psql.connect)):
    phone = db.query(models.PhonePool).filter(
        models.PhonePool.phone == number).one_or_none()
    if phone == None:
        return

    if phone.question_type == "yes":
        question = db.query(
            models.Questions).filter(
            models.Questions.voteYesPhone == number).one_or_none()
        qbuilder = QuestionBuilder(
            question.yes + 1,
            question.no,
            heroku_url,
            question.uuid)
        question.yes += 1
    else:
        question = db.query(
            models.Questions).filter(
            models.Questions.voteNoPhone == number).one_or_none()
        qbuilder = QuestionBuilder(
            question.yes,
            question.no + 1,
            heroku_url,
            question.uuid)
        question.no += 1

    qbuilder.commit()
    db.add(question)
    db.commit()
    db.refresh(question)

    return question


@app.get("/api/questions")
def all_questions(db: Session = Depends(psql.connect)):
    return db.query(models.Questions).all()


@app.post("/api/question")
def add_question(question: Question, db: Session = Depends(psql.connect)):
    phones = get_free_phones(db)
    if (len(phones) < 2):
        return "No enough free phones (needs >= 2)."

    quuid = uuid.uuid4()
    yes, no = phones
    question = models.Questions(
        prompt=question.description,
        uuid=str(quuid),
        url=heroku_url + "vxml/" + str(quuid) + ".xml",
        voteYesPhone=yes.phone,
        voteNoPhone=no.phone,
        yes=0,
        no=0,
    )

    qbuilder = QuestionBuilder(0, 0, heroku_url, str(quuid))
    qbuilder.commit()

    combine_phone_question(yes, str(quuid), "yes", db)
    combine_phone_question(no, str(quuid), "no", db)

    vxml = HomeBuilder()
    # updated_vxml = vxml.delete_menu_option(9)
    options = {}
    options["prompt"] = question.prompt
    options["url"] = question.url
    options["audio_url"] = heroku_url + "audios/" + str(question.uuid)

    vxml.updated_vxml([options])
    vxml.commit()

    db.add(question)
    db.commit()
    db.refresh(question)
    return question


def get_free_phones(db: Session = Depends(psql.connect)):
    phones = db.query(models.PhonePool).filter(
        models.PhonePool.question_uuid == None).all()
    if (len(phones) <= 1):
        return []
    else:
        return [phones[0], phones[1]]


def combine_phone_question(
    phone: models.PhonePool,
    quuid: str,
    qtype: str,
    db: Session = Depends(
        psql.connect)):
    setattr(phone, "question_uuid", quuid)
    setattr(phone, "question_type", qtype)

    db.add(phone)
    db.commit()
    db.refresh(phone)


@app.get("/vxml/{path}", response_class=FileResponse)
def fetch_files(path: str):
    # Decode URL-encoded characters
    decoded_string = urllib.parse.unquote(path)
    return "vxml/" + decoded_string


@app.get("/audios/{path}", response_class=FileResponse)
def fetch_files(path: str):
    # Decode URL-encoded characters
    decoded_string = urllib.parse.unquote(path)
    return "audios/" + decoded_string


@app.get("/api/reset")
def reset(db: Session = Depends(psql.connect)):
    root = """
        <vxml xmlns="http://www.w3.org/2001/vxml" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" version="2.0" xsi:schemaLocation="http://www.w3.org/2001/vxml              http://www.w3.org/TR/voicexml20/vxml.xsd">
            <menu>
            <prompt>
            </prompt>
            <noinput>Please say one of <enumerate /></noinput>
            </menu> 
        </vxml>
        """
    delete_files("vxml")
    delete_files("audios")

    db.query(models.PhonePool).delete()
    db.commit()

    db.query(models.Questions).delete()
    db.commit()

    with open("vxml/root.xml", "w") as f:
        f.write(root) 

    return "cleaned"

def delete_files(directory):
    if not os.path.isdir(directory):
        print(f"The directory {directory} does not exist.")
        return

    # Loop through all files in the directory
    for filename in os.listdir(directory):
        file_path = os.path.join(directory, filename)
        
        # Check if it is a file and not a directory
        if os.path.isfile(file_path):
            os.remove(file_path)  # Remove the file
            print(f"Deleted file: {file_path}")
        else:
            print(f"Skipped directory: {file_path}")

@app.post("/api/question/{language}")
def add_question(
    language: str,
    question: Question,
    db: Session = Depends(
        psql.connect)):
    prompt = question.description
    id = question.uuid

    tts = ICT4DTTS(openai_api=openai_api)
    tts.english_text_to_speech(
        prompt,
        language,
        is_generate_wav_file=True,
        file_path="audios/" +
        str(id) +
        "-" +
        language +
        ".mp3")
    return language + " Added"
