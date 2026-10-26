import os
from uuid import uuid4

import db
from psycopg.sql import SQL,Identifier


def use_schema(case):
    previous=os.environ.get('PGSCHEMA')
    name='test_'+uuid4().hex
    os.environ['PGSCHEMA']=name
    db.init_schema()
    def cleanup():
        conn=db.raw_connect()
        try:
            conn.execute(SQL('DROP SCHEMA {} CASCADE').format(Identifier(name)))
            conn.commit()
        finally:conn.close()
        if previous is None:os.environ.pop('PGSCHEMA',None)
        else:os.environ['PGSCHEMA']=previous
    case.addCleanup(cleanup)
