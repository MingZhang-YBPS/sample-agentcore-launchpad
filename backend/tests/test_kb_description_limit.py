"""A KB description is capped at Bedrock's 200 characters (BUG-10).

The console used to allow 1000, so a description between 201 and 1000 characters
passed validation here and failed at CreateKnowledgeBase with a bare AWS
"invalid parameter" error.
"""

import pytest
from pydantic import ValidationError

from app.routers.knowledge import KB_DESCRIPTION_MAX, CreateKBRequest, PatchKBRequest


def test_the_limit_is_the_aws_one():
    assert KB_DESCRIPTION_MAX == 200


@pytest.mark.parametrize("model", [
    lambda d: CreateKBRequest(name="kb-1", description=d),
    lambda d: PatchKBRequest(description=d),
])
def test_200_passes_201_is_refused(model):
    model("界" * 200)
    with pytest.raises(ValidationError):
        model("界" * 201)
