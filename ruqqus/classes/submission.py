from flask import render_template, request, abort
import time
from sqlalchemy import *
from sqlalchemy.orm import relationship, deferred
import math
from urllib.parse import urlparse
import random
from os import environ

from ruqqus.helpers.base36 import *
from ruqqus.helpers.lazy import lazy
import ruqqus.helpers.aws as aws
from ruqqus.__main__ import Base, db, cache
from .votes import Vote
from .domains import Domain
from .flags import Flag

class Submission(Base):
 
    __tablename__="submissions"

    id = Column(BigInteger, primary_key=True)
    author_id = Column(BigInteger, ForeignKey("users.id"))
    title = Column(String(500), default=None)
    url = Column(String(500), default=None)
    edited_utc = Column(BigInteger, default=0)
    created_utc = Column(BigInteger, default=0)
    is_banned = Column(Boolean, default=False)
    is_deleted=Column(Boolean, default=False)
    distinguish_level=Column(Integer, default=0)
    created_str=Column(String(255), default=None)
    stickied=Column(Boolean, default=False)
    comments=relationship("Comment", lazy="dynamic", backref="submissions")
    body=Column(String(2000), default="")
    body_html=Column(String(2200), default="")
    embed_url=Column(String(256), default="")
    domain_ref=Column(Integer, ForeignKey("domains.id"))
    flags=relationship("Flag", lazy="dynamic", backref="submission")
    is_approved=Column(Integer, ForeignKey("users.id"), default=0)
    approved_utc=Column(Integer, default=0)
    board_id=Column(Integer, ForeignKey("boards.id"), default=None)
    original_board_id=Column(Integer, ForeignKey("boards.id"), default=None)
    over_18=Column(Boolean, default=False)
    original_board=relationship("Board", uselist=False, primaryjoin="Board.id==Submission.original_board_id")
    ban_reason=Column(String(128), default="")
    creation_ip=Column(String(64), default="")
    mod_approved=Column(Integer, default=None)
    is_image=Column(Boolean, default=False)
    has_thumb=Column(Boolean, default=False)    

    approved_by=relationship("User", uselist=False, primaryjoin="Submission.is_approved==User.id")


    #These are virtual properties handled as postgres functions server-side
    #There is no difference to SQLAlchemy, but they cannot be written to

    ups = deferred(Column(Integer, server_default=FetchedValue()))
    downs=deferred(Column(Integer, server_default=FetchedValue()))
    age=deferred(Column(Integer, server_default=FetchedValue()))
    comment_count=Column(Integer, server_default=FetchedValue())
    flag_count=deferred(Column(Integer, server_default=FetchedValue()))
    report_count=deferred(Column(Integer, server_default=FetchedValue()))
    score=Column(Float, server_default=FetchedValue())
    

    def __init__(self, *args, **kwargs):

        if "created_utc" not in kwargs:
            kwargs["created_utc"]=int(time.time())
            kwargs["created_str"]=time.strftime("%I:%M %p on %d %b %Y", time.gmtime(kwargs["created_utc"]))

        kwargs["creation_ip"]=request.remote_addr

        super().__init__(*args, **kwargs)
        
    def __repr__(self):
        return f"<Submission(id={self.id})>"


    @property
    #@cache.memoize(timeout=60)
    def domain_obj(self):
        if not self.domain_ref:
            return None
        
        return db.query(Domain).filter_by(id=self.domain_ref).first()


    @property
    @cache.memoize(timeout=60)
    def score_percent(self):
        try:
            return int((self.ups/(self.ups+self.downs))*100)
        except ZeroDivisionError:
            return 0

    @property
    def base36id(self):
        return base36encode(self.id)

    @property
    def fullname(self):
        return f"t2_{self.base36id}"

    @property
    def permalink(self):
        return f"/post/{self.base36id}"
                                      
    def rendered_page(self, comment=None, comment_info=None, v=None):

        #check for banned
        if self.is_banned or self.is_deleted:
            if v and v.admin_level>=3:
                template="submission.html"
            else:
                template="submission_banned.html"
        else:
            template="submission.html"

        #load and tree comments
        #calling this function with a comment object will do a comment permalink thing
        self.tree_comments(comment=comment)
        
        #return template
        return render_template(template,
                               v=v,
                               p=self,
                               sort_method=request.args.get("sort","Hot").capitalize(),
                               linked_comment=comment,
                               comment_info=comment_info)

    @property
    @lazy
    def author(self):
        return self.author_rel


    @property
    @lazy
    def domain(self):

        if not self.url:
            return "text post"
        domain= urlparse(self.url).netloc
        if domain.startswith("www."):
            domain=domain.split("www.")[1]
        return domain
    
    @property
    def score_fuzzed(self, k=0.01):
        real=self.score
        a=math.floor(real*(1-k))
        b=math.ceil(real*(1+k))
        return random.randint(a,b)        

    def tree_comments(self, comment=None):

        def tree_replies(thing, layer=1):

            thing.__dict__["replies"]=[]
            i=len(comments)-1
        
            while i>=0:
                if comments[i].parent_fullname==thing.fullname:
                    thing.__dict__["replies"].append(comments[i])
                    comments.pop(i)

                i-=1
                
            if layer <=8:
                for reply in thing.replies:
                    tree_replies(reply, layer=layer+1)
                
        ######
                
        if comment:
            self.replies=[comment]
            return



        #get sort type
        sort_type = request.args.get("sort","hot")


        #Treeing is done from the end because reasons, so these sort orders are reversed
        if sort_type=="hot":
            comments=self.comments.order_by(text("comments.rank_hot ASC")).all()
        elif sort_type=="top":
            comments=self.comments.order_by(text("comments.score ASC")).all()
        elif sort_type=="new":
            comments=self.comments.order_by(text("comments.created_utc")).all()
        elif sort_type=="disputed":
            comments=self.comments.order_by(text("comments.rank_fiery ASC")).all()
        elif sort_type=="random":
            c=self.comments.all()
            comments=random.sample(c, k=len(c))
        else:
            abort(422)



        tree_replies(self)

        

        
    @property
    def age_string(self):

        age=self.age

        if age<60:
            return "just now"
        elif age<3600:
            minutes=int(age/60)
            return f"{minutes} minute{'s' if minutes>1 else ''} ago"
        elif age<86400:
            hours=int(age/3600)
            return f"{hours} hour{'s' if hours>1 else ''} ago"
        elif age<2592000:
            days=int(age/86400)
            return f"{days} day{'s' if days>1 else ''} ago"

        now=time.gmtime()
        ctd=time.gmtime(self.created_utc)
        months=now.tm_mon-ctd.tm_mon+12*(now.tm_year-ctd.tm_year)

        if months < 12:
            return f"{months} month{'s' if months>1 else ''} ago"
        else:
            years=int(months/12)
            return f"{years} year{'s' if years>1 else ''} ago"

    @property
    def edited_string(self):

        age=int(time.time())-self.edited_utc

        if age<60:
            return "just now"
        elif age<3600:
            minutes=int(age/60)
            return f"{minutes} minute{'s' if minutes>1 else ''} ago"
        elif age<86400:
            hours=int(age/3600)
            return f"{hours} hour{'s' if hours>1 else ''} ago"
        elif age<2592000:
            days=int(age/86400)
            return f"{days} day{'s' if days>1 else ''} ago"

        now=time.gmtime()
        ctd=time.gmtime(self.created_utc)
        months=now.tm_mon-ctd.tm_mon+12*(now.tm_year-ctd.tm_year)

        if months < 12:
            return f"{months} month{'s' if months>1 else ''} ago"
        else:
            years=now.tm_year-ctd.tm_year
            return f"{years} year{'s' if years>1 else ''} ago"
        

    @property
    def created_date(self):
        return time.strftime("%d %B %Y", time.gmtime(self.created_utc))

    @property
    def edited_date(self):
        return time.strftime("%d %B %Y", time.gmtime(self.edited_utc))

    @property
    def active_flags(self):
        if self.is_approved:
            return 0
        else:
            return self.flags.filter(Flag.created_utc>self.approved_utc).count()

    def save_thumb(self):

        url=f"https://api.apiflash.com/v1/urltoimage"
        params={'access_key':environ.get("APIFLASH_KEY"),
                'format':'png',
                'height':720,
                'response_type':'image',
                'thumbnail_width':300,
                'url':self.url
                }
        x=requests.get(url, params=params)
        print("have thumb from apiflash")

        tempname=name.replace("/","_")

        with open(tempname, "wb") as file:
            for chunk in r.iter_content(1024):
                f.write(chunk)

        print("thumb saved")

        aws.upload_from_file(f"posts/{self.base36id}/thumb.png", tempname)
        self.has_thumb=True
        db.add(self)
        db.commit()

        print("thumb all success")
        

    @property
    @lazy
    def thumb_url(self):
    
        if self.has_thumb:
            return f"https://s3.us-east-2.amazonaws.com/i.ruqqus.com/posts/{self.base36id}/thumb.png"
        elif self.is_image:
            return self.url
        else:
            return None
