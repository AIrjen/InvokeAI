"""This module manages the InvokeAI `models.yaml` file, mapping
symbolic diffusers model names to the paths and repo_ids used
by the underlying `from_pretrained()` call.

For fetching models, use manager.get_model('symbolic name'). This will
return a SDModelInfo object that contains the following attributes:
   
   * context -- a context manager Generator that loads and locks the 
                model into GPU VRAM and returns the model for use. 
                See below for usage.
   * name -- symbolic name of the model
   * hash -- unique hash for the model
   * location -- path or repo_id of the model
   * revision -- revision of the model if coming from a repo id,
                e.g. 'fp16'
   * precision -- torch precision of the model
   * status -- a ModelStatus enum corresponding to one of
               'not_loaded', 'in_ram', 'in_vram' or 'active'

Typical usage:

   from invokeai.backend import ModelManager
   manager = ModelManager(config_path='./configs/models.yaml',max_models=4)
   model_info = manager.get_model('stable-diffusion-1.5')
   with model_info.context as my_model:
      my_model.latents_from_embeddings(...)

The manager uses the underlying ModelCache class to keep
frequently-used models in RAM and move them into GPU as needed for
generation operations. The ModelCache object can be accessed using 
the manager's "cache" attribute.

Other methods provided by ModelManager support importing, editing,
converting and deleting models.

The general format of a models.yaml section is:

 name-of-model:
     format: diffusers|ckpt|vae|text_encoder|tokenizer...
     repo_id: owner/repo
     path: /path/to/local/file/or/directory
     subfolder: subfolder-name
     submodel: vae|text_encoder|tokenizer...

The format is one of {diffusers, ckpt, vae, text_encoder, tokenizer,
unet, scheduler, safety_checker, feature_extractor}, and correspond to
items in the SDModelType enum defined in model_cache.py

One, but not both, of repo_id and path are provided. repo_id is the
HuggingFace repository ID of the model, and path points to the file or
directory on disk.

If subfolder is provided, then the model exists in a subdirectory of
the main model. These are usually named after the model type, such as
"unet".

Finally, if submodel is provided, then the path/repo_id is treated as
a diffusers model, the whole thing is ready into memory, and then the
requested part (e.g. "unet") is retrieved.

This summarizes the three ways of getting a non-diffuser model:

 clip-test-1:
   format: text_encoder
   repo_id: openai/clip-vit-large-patch14
   description: Returns standalone CLIPTextModel

 clip-test-2:
   format: diffusers
   repo_id: stabilityai/stable-diffusion-2
   submodel: text_encoder
   description: Returns the text_encoder part of whole diffusers model (whole thing in RAM)

 clip-test-3:
   format: text_encoder
   repo_id: stabilityai/stable-diffusion-2
   subfolder: text_encoder
   description: Returns the text_encoder in the subfolder of the diffusers model (just the encoder in RAM)

 clip-token:
   format: tokenizer
   repo_id: openai/clip-vit-large-patch14
   description: Returns standalone tokenizer
"""
from __future__ import annotations

import os
import re
import textwrap
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path
from shutil import rmtree
from typing import Union, Callable, types

import safetensors
import safetensors.torch
import torch
import invokeai.backend.util.logging as logger
from huggingface_hub import scan_cache_dir
from omegaconf import OmegaConf
from omegaconf.dictconfig import DictConfig

from invokeai.backend.globals import Globals, global_cache_dir, global_resolve_path
from .model_cache import ModelClass, ModelCache, ModelLocker, SDModelType, ModelStatus, LegacyInfo

from ..util import CUDA_DEVICE

# wanted to use pydantic here, but Generator objects not supported
@dataclass
class SDModelInfo():
    context: ModelLocker
    name: str
    hash: str
    location: Union[Path,str]
    precision: torch.dtype
    subfolder: Path = None
    revision: str = None
    _cache: ModelCache = None

    @property
    def status(self)->ModelStatus:
        '''Return load status of this model as a model_cache.ModelStatus enum'''
        if not self._cache:
            return ModelStatus.unknown
        return self._cache.status(
            self.location,
            revision = self.revision,
            subfolder = self.subfolder
        )

class InvalidModelError(Exception):
    "Raised when an invalid model is requested"
    pass

class SDLegacyType(Enum):
    V1 = auto()
    V1_INPAINT = auto()
    V2 = auto()
    V2_e = auto()
    V2_v = auto()
    UNKNOWN = auto()

DEFAULT_MAX_MODELS = 2

class ModelManager(object):
    """
    High-level interface to model management.
    """

    logger: types.ModuleType = logger

    def __init__(
            self,
            config_path: Path,
            device_type: torch.device = CUDA_DEVICE,
            precision: torch.dtype = torch.float16,
            max_loaded_models=DEFAULT_MAX_MODELS,
            sequential_offload=False,
            logger: types.ModuleType = logger,
    ):
        """
        Initialize with the path to the models.yaml config file. 
        Optional parameters are the torch device type, precision, max_models,
        and sequential_offload boolean. Note that the default device
        type and precision are set up for a CUDA system running at half precision.
        """
        self.config_path = config_path
        self.config = OmegaConf.load(self.config_path)
        self.cache = ModelCache(
            max_models=max_loaded_models,
            execution_device = device_type,
            precision = precision,
            sequential_offload = sequential_offload,
            logger = logger,
        )
        self.cache_keys = dict()
        self.logger = logger

    def valid_model(self, model_name: str) -> bool:
        """
        Given a model name, returns True if it is a valid
        identifier.
        """
        return model_name in self.config

    def get_model(self,
                  model_name: str = None,
                  submodel: SDModelType=None,
                  ) -> SDModelInfo:
        """Given a model named identified in models.yaml, return
        an SDModelInfo object describing it.
        :param model_name: symbolic name of the model in models.yaml
        :param submodel: an SDModelType enum indicating the portion of 
               the model to retrieve (e.g. SDModelType.vae)
        """
        if not model_name:
            model_name = self.default_model()

        if not self.valid_model(model_name):
            raise InvalidModelError(
                f'"{model_name}" is not a known model name. Please check your models.yaml file'
            )

        # get the required loading info out of the config file
        mconfig = self.config[model_name]
        
        format = mconfig.get('format','diffusers')
        model_type = SDModelType.diffusion_pipeline
        model_parts = dict([(x.name,x) for x in SDModelType])
        legacy = None
        
        if format=='ckpt':
            location = global_resolve_path(mconfig.weights)
            legacy = LegacyInfo(
                config_file = global_resolve_path(mconfig.config),
            )
            if mconfig.get('vae'):
                legacy.vae_file = global_resolve_path(mconfig.vae)
        elif format=='diffusers':
            location = mconfig.get('repo_id') or mconfig.get('path')
            if sm := mconfig.get('submodel'):
                submodel = model_parts[sm]
        elif format in model_parts:
            location = mconfig.get('repo_id') or mconfig.get('path') or mconfig.get('weights')
            model_type = model_parts[format]
        else:
            raise InvalidModelError(
                f'"{model_name}" has an unknown format {format}'
            )
            
        subfolder = mconfig.get('subfolder')
        revision = mconfig.get('revision')
        hash = self.cache.model_hash(location,revision)

        # to support the traditional way of attaching a VAE
        # to a model, we hacked in `attach_model_part`
        vae = (None,None)
        try:
            vae_id = mconfig.vae.repo_id
            vae = (SDModelType.vae,vae_id)
        except Exception:
            pass
        model_context = self.cache.get_model(
            location,
            model_type = model_type,
            revision = revision,
            subfolder = subfolder,
            legacy_info = legacy,
            submodel = submodel,
            attach_model_part=vae,
        )

        # in case we need to communicate information about this
        # model to the cache manager, then we need to remember
        # the cache key
        self.cache_keys[model_name] = model_context.key
        
        return SDModelInfo(
            context = model_context,
            name = model_name,
            hash = hash,
            location = location,
            revision = revision,
            precision = self.cache.precision,
            subfolder = subfolder,
            _cache = self.cache
        )

    def default_model(self) -> str | None:
        """
        Returns the name of the default model, or None
        if none is defined.
        """
        for model_name in self.config:
            if self.config[model_name].get("default"):
                return model_name
        return list(self.config.keys())[0]  # first one

    def set_default_model(self, model_name: str) -> None:
        """
        Set the default model. The change will not take
        effect until you call model_manager.commit()
        """
        assert model_name in self.model_names(), f"unknown model '{model_name}'"

        config = self.config
        for model in config:
            config[model].pop("default", None)
        config[model_name]["default"] = True

    def model_info(self, model_name: str) -> dict:
        """
        Given a model name returns the OmegaConf (dict-like) object describing it.
        """
        if model_name not in self.config:
            return None
        return self.config[model_name]

    def model_names(self) -> list[str]:
        """
        Return a list consisting of all the names of models defined in models.yaml
        """
        return list(self.config.keys())

    def is_legacy(self, model_name: str) -> bool:
        """
        Return true if this is a legacy (.ckpt) model
        """
        # if we are converting legacy files automatically, then
        # there are no legacy ckpts!
        if Globals.ckpt_convert:
            return False
        info = self.model_info(model_name)
        if "weights" in info and info["weights"].endswith((".ckpt", ".safetensors")):
            return True
        return False

    def list_models(self) -> dict:
        """
        Return a dict of models in the format:
        { model_name1: {'status': ('active'|'cached'|'not loaded'),
                        'description': description,
                        'format': ('ckpt'|'diffusers'|'vae'),
                       },
          model_name2: { etc }
        Please use model_manager.models() to get all the model names,
        model_manager.model_info('model-name') to get the stanza for the model
        named 'model-name', and model_manager.config to get the full OmegaConf
        object derived from models.yaml
        """
        models = {}
        for name in sorted(self.config, key=str.casefold):
            stanza = self.config[name]

            # don't include VAEs in listing (legacy style)
            if "config" in stanza and "/VAE/" in stanza["config"]:
                continue

            models[name] = dict()
            format = stanza.get("format", "ckpt")  # Determine Format

            # Common Attribs
            status = self.cache.status(
                stanza.get('weights') or stanza.get('repo_id'),
                revision=stanza.get('revision'),
                subfolder=stanza.get('subfolder')
            )
            description = stanza.get("description", None)
            models[name].update(
                description=description,
                format=format,
                status=status.value
            )


            # Checkpoint Config Parse
            if format == "ckpt":
                models[name].update(
                    config=str(stanza.get("config", None)),
                    weights=str(stanza.get("weights", None)),
                    vae=str(stanza.get("vae", None)),
                    width=str(stanza.get("width", 512)),
                    height=str(stanza.get("height", 512)),
                )

            # Diffusers Config Parse
            if vae := stanza.get("vae", None):
                if isinstance(vae, DictConfig):
                    vae = dict(
                        repo_id=str(vae.get("repo_id", None)),
                        path=str(vae.get("path", None)),
                        subfolder=str(vae.get("subfolder", None)),
                    )

            if format == "diffusers":
                models[name].update(
                    vae=vae,
                    repo_id=str(stanza.get("repo_id", None)),
                    path=str(stanza.get("path", None)),
                )

        return models

    def print_models(self) -> None:
        """
        Print a table of models, their descriptions, and load status
        """
        models = self.list_models()
        for name in models:
            if models[name]["format"] == "vae":
                continue
            line = f'{name:25s} {models[name]["status"]:>15s}  {models[name]["format"]:10s} {models[name]["description"]}'
            if models[name]["status"] == "active":
                line = f"\033[1m{line}\033[0m"
            print(line)

    def del_model(self, model_name: str, delete_files: bool = False) -> None:
        """
        Delete the named model.
        """
        omega = self.config
        if model_name not in omega:
            self.logger.error(f"Unknown model {model_name}")
            return
        # save these for use in deletion later
        conf = omega[model_name]
        repo_id = conf.get("repo_id", None)
        path = self._abs_path(conf.get("path", None))
        weights = self._abs_path(conf.get("weights", None))

        del omega[model_name]
        if model_name in self.stack:
            self.stack.remove(model_name)
        if delete_files:
            if weights:
                self.logger.info(f"Deleting file {weights}")
                Path(weights).unlink(missing_ok=True)
            elif path:
                self.logger.info(f"Deleting directory {path}")
                rmtree(path, ignore_errors=True)
            elif repo_id:
                self.logger.info(f"Deleting the cached model directory for {repo_id}")
                self._delete_model_from_cache(repo_id)

    def add_model(
        self, model_name: str, model_attributes: dict, clobber: bool = False
    ) -> None:
        """
        Update the named model with a dictionary of attributes. Will fail with an
        assertion error if the name already exists. Pass clobber=True to overwrite.
        On a successful update, the config will be changed in memory and the
        method will return True. Will fail with an assertion error if provided
        attributes are incorrect or the model name is missing.
        """
        omega = self.config

        assert "format" in model_attributes, 'missing required field "format"'
        if model_attributes["format"] == "diffusers":
            assert (
                "description" in model_attributes
            ), 'required field "description" is missing'
            assert (
                "path" in model_attributes or "repo_id" in model_attributes
            ), 'model must have either the "path" or "repo_id" fields defined'
        elif model_attributes["format"] == "ckpt":
            for field in ("description", "weights", "height", "width", "config"):
                assert field in model_attributes, f"required field {field} is missing"
        else:
            assert "weights" in model_attributes and "description" in model_attributes

        assert (
            clobber or model_name not in omega
        ), f'attempt to overwrite existing model definition "{model_name}"'

        omega[model_name] = model_attributes

        if "weights" in omega[model_name]:
            omega[model_name]["weights"].replace("\\", "/")
            
        if clobber and model_name in self.cache_keys:
            self.cache.uncache_model(self.cache_keys[model_name])
            del self.cache_keys[model_name]

    def import_diffuser_model(
        self,
        repo_or_path: Union[str, Path],
        model_name: str = None,
        description: str = None,
        vae: dict = None,
        commit_to_conf: Path = None,
    ) -> bool:
        """
        Attempts to install the indicated diffuser model and returns True if successful.

        "repo_or_path" can be either a repo-id or a path-like object corresponding to the
        top of a downloaded diffusers directory.

        You can optionally provide a model name and/or description. If not provided,
        then these will be derived from the repo name. If you provide a commit_to_conf
        path to the configuration file, then the new entry will be committed to the
        models.yaml file.
        """
        model_name = model_name or Path(repo_or_path).stem
        model_description = description or f"Imported diffusers model {model_name}"
        new_config = dict(
            description=model_description,
            vae=vae,
            format="diffusers",
        )
        if isinstance(repo_or_path, Path) and repo_or_path.exists():
            new_config.update(path=str(repo_or_path))
        else:
            new_config.update(repo_id=repo_or_path)

        self.add_model(model_name, new_config, True)
        if commit_to_conf:
            self.commit(commit_to_conf)
        return model_name

    def import_lora(
            self,
            path: Path,
            model_name: str=None,
            description: str=None,
    ):
        """
        Creates an entry for the indicated lora file. Call
        mgr.commit() to write out the configuration to models.yaml
        """
        path = Path(path)
        model_name = model_name or path.stem
        model_description = description or f"LoRA model {model_name}"
        self.add_model(model_name,
                       dict(
                           format="lora",
                           weights=str(path),
                           description=model_description,
                           ),
                       True
                       )
        
    def import_embedding(
            self,
            path: Path,
            model_name: str=None,
            description: str=None,
    ):
        """
        Creates an entry for the indicated lora file. Call
        mgr.commit() to write out the configuration to models.yaml
        """
        path = Path(path)
        if path.is_directory() and (path / "learned_embeds.bin").exists():
            weights = path / "learned_embeds.bin"
        else:
            weights = path
            
        model_name = model_name or path.stem
        model_description = description or f"Textual embedding model {model_name}"
        self.add_model(model_name,
                       dict(
                           format="textual_inversion",
                           weights=str(path),
                           description=model_description,
                           ),
                       True
                       )

    @classmethod
    def probe_model_type(self, checkpoint: dict) -> SDLegacyType:
        """
        Given a pickle or safetensors model object, probes contents
        of the object and returns an SDLegacyType indicating its
        format. Valid return values include:
        SDLegacyType.V1
        SDLegacyType.V1_INPAINT
        SDLegacyType.V2     (V2 prediction type unknown)
        SDLegacyType.V2_e   (V2 using 'epsilon' prediction type)
        SDLegacyType.V2_v   (V2 using 'v_prediction' prediction type)
        SDLegacyType.UNKNOWN
        """
        global_step = checkpoint.get("global_step")
        state_dict = checkpoint.get("state_dict") or checkpoint

        try:
            key_name = "model.diffusion_model.input_blocks.2.1.transformer_blocks.0.attn2.to_k.weight"
            if key_name in state_dict and state_dict[key_name].shape[-1] == 1024:
                if global_step == 220000:
                    return SDLegacyType.V2_e
                elif global_step == 110000:
                    return SDLegacyType.V2_v
                else:
                    return SDLegacyType.V2
            # otherwise we assume a V1 file
            in_channels = state_dict[
                "model.diffusion_model.input_blocks.0.0.weight"
            ].shape[1]
            if in_channels == 9:
                return SDLegacyType.V1_INPAINT
            elif in_channels == 4:
                return SDLegacyType.V1
            else:
                return SDLegacyType.UNKNOWN
        except KeyError:
            return SDLegacyType.UNKNOWN

    def heuristic_import(
        self,
        path_url_or_repo: str,
        model_name: str = None,
        description: str = None,
        model_config_file: Path = None,
        commit_to_conf: Path = None,
        config_file_callback: Callable[[Path], Path] = None,
    ) -> str:
        """Accept a string which could be:
           - a HF diffusers repo_id
           - a URL pointing to a legacy .ckpt or .safetensors file
           - a local path pointing to a legacy .ckpt or .safetensors file
           - a local directory containing .ckpt and .safetensors files
           - a local directory containing a diffusers model

        After determining the nature of the model and downloading it
        (if necessary), the file is probed to determine the correct
        configuration file (if needed) and it is imported.

        The model_name and/or description can be provided. If not, they will
        be generated automatically.

        If commit_to_conf is provided, the newly loaded model will be written
        to the `models.yaml` file at the indicated path. Otherwise, the changes
        will only remain in memory.

        The routine will do its best to figure out the config file
        needed to convert legacy checkpoint file, but if it can't it
        will call the config_file_callback routine, if provided. The
        callback accepts a single argument, the Path to the checkpoint
        file, and returns a Path to the config file to use.

        The (potentially derived) name of the model is returned on
        success, or None on failure. When multiple models are added
        from a directory, only the last imported one is returned.

        """
        model_path: Path = None
        thing = path_url_or_repo  # to save typing

        self.logger.info(f"Probing {thing} for import")

        if thing.startswith(("http:", "https:", "ftp:")):
            self.logger.info(f"{thing} appears to be a URL")
            model_path = self._resolve_path(
                thing, "models/ldm/stable-diffusion-v1"
            )  # _resolve_path does a download if needed

        elif Path(thing).is_file() and thing.endswith((".ckpt", ".safetensors")):
            if Path(thing).stem in ["model", "diffusion_pytorch_model"]:
                self.logger.debug(f"{Path(thing).name} appears to be part of a diffusers model. Skipping import")
                return
            else:
                self.logger.debug(f"{thing} appears to be a checkpoint file on disk")
                model_path = self._resolve_path(thing, "models/ldm/stable-diffusion-v1")

        elif Path(thing).is_dir() and Path(thing, "model_index.json").exists():
            self.logger.debug(f"{thing} appears to be a diffusers file on disk")
            model_name = self.import_diffuser_model(
                thing,
                vae=dict(repo_id="stabilityai/sd-vae-ft-mse"),
                model_name=model_name,
                description=description,
                commit_to_conf=commit_to_conf,
            )

        elif Path(thing).is_dir():
            if (Path(thing) / "model_index.json").exists():
                self.logger.debug(f"{thing} appears to be a diffusers model.")
                model_name = self.import_diffuser_model(
                    thing, commit_to_conf=commit_to_conf
                )
            else:
                self.logger.debug(f"{thing} appears to be a directory. Will scan for models to import")
                for m in list(Path(thing).rglob("*.ckpt")) + list(
                    Path(thing).rglob("*.safetensors")
                ):
                    if model_name := self.heuristic_import(
                        str(m), commit_to_conf=commit_to_conf
                    ):
                        self.logger.info(f"{model_name} successfully imported")
                return model_name

        elif re.match(r"^[\w.+-]+/[\w.+-]+$", thing):
            self.logger.debug(f"{thing} appears to be a HuggingFace diffusers repo_id")
            model_name = self.import_diffuser_model(
                thing, commit_to_conf=commit_to_conf
            )
            pipeline, _, _, _ = self._load_diffusers_model(self.config[model_name])
            return model_name
        else:
            self.logger.warning(f"{thing}: Unknown thing. Please provide a URL, file path, directory or HuggingFace repo_id")

        # Model_path is set in the event of a legacy checkpoint file.
        # If not set, we're all done
        if not model_path:
            return

        if model_path.stem in self.config:  # already imported
            self.logger.debug("Already imported. Skipping")
            return model_path.stem

        # another round of heuristics to guess the correct config file.
        checkpoint = None
        if model_path.suffix in [".ckpt", ".pt"]:
            self.scan_model(model_path, model_path)
            checkpoint = torch.load(model_path)
        else:
            checkpoint = safetensors.torch.load_file(model_path)

        # additional probing needed if no config file provided
        if model_config_file is None:
            # look for a like-named .yaml file in same directory
            if model_path.with_suffix(".yaml").exists():
                model_config_file = model_path.with_suffix(".yaml")
                self.logger.debug(f"Using config file {model_config_file.name}")

            else:
                model_type = self.probe_model_type(checkpoint)
                if model_type == SDLegacyType.V1:
                    self.logger.debug("SD-v1 model detected")
                    model_config_file = Path(
                        Globals.root, "configs/stable-diffusion/v1-inference.yaml"
                    )
                elif model_type == SDLegacyType.V1_INPAINT:
                    self.logger.debug("SD-v1 inpainting model detected")
                    model_config_file = Path(
                        Globals.root,
                        "configs/stable-diffusion/v1-inpainting-inference.yaml",
                    )
                elif model_type == SDLegacyType.V2_v:
                    self.logger.debug("SD-v2-v model detected")
                    model_config_file = Path(
                        Globals.root, "configs/stable-diffusion/v2-inference-v.yaml"
                    )
                elif model_type == SDLegacyType.V2_e:
                    self.logger.debug("SD-v2-e model detected")
                    model_config_file = Path(
                        Globals.root, "configs/stable-diffusion/v2-inference.yaml"
                    )
                elif model_type == SDLegacyType.V2:
                    self.logger.warning(
                        f"{thing} is a V2 checkpoint file, but its parameterization cannot be determined. Please provide configuration file path."
                    )
                    return
                else:
                    self.logger.warning(
                        f"{thing} is a legacy checkpoint file but not a known Stable Diffusion model. Please provide configuration file path."
                    )
                    return

        if not model_config_file and config_file_callback:
            model_config_file = config_file_callback(model_path)

        # despite our best efforts, we could not find a model config file, so give up
        if not model_config_file:
            return

        # look for a custom vae, a like-named file ending with .vae in the same directory
        vae_path = None
        for suffix in ["pt", "ckpt", "safetensors"]:
            if (model_path.with_suffix(f".vae.{suffix}")).exists():
                vae_path = model_path.with_suffix(f".vae.{suffix}")
                self.logger.debug(f"Using VAE file {vae_path.name}")
        vae = None if vae_path else dict(repo_id="stabilityai/sd-vae-ft-mse")

        diffuser_path = Path(
            Globals.root, "models", Globals.converted_ckpts_dir, model_path.stem
        )
        model_name = self.convert_and_import(
            model_path,
            diffusers_path=diffuser_path,
            vae=vae,
            vae_path=str(vae_path),
            model_name=model_name,
            model_description=description,
            original_config_file=model_config_file,
            commit_to_conf=commit_to_conf,
            scan_needed=False,
        )
        return model_name

    def convert_and_import(
        self,
        ckpt_path: Path,
        diffusers_path: Path,
        model_name=None,
        model_description=None,
        vae: dict = None,
        vae_path: Path = None,
        original_config_file: Path = None,
        commit_to_conf: Path = None,
        scan_needed: bool = True,
    ) -> str:
        """
        Convert a legacy ckpt weights file to diffuser model and import
        into models.yaml.
        """
        ckpt_path = self._resolve_path(ckpt_path, "models/ldm/stable-diffusion-v1")
        if original_config_file:
            original_config_file = self._resolve_path(
                original_config_file, "configs/stable-diffusion"
            )

        new_config = None

        if diffusers_path.exists():
            self.logger.error(
                f"The path {str(diffusers_path)} already exists. Please move or remove it and try again."
            )
            return

        model_name = model_name or diffusers_path.name
        model_description = model_description or f"Converted version of {model_name}"
        self.logger.debug(f"Converting {model_name} to diffusers (30-60s)")

        # to avoid circular import errors
        from .convert_ckpt_to_diffusers import convert_ckpt_to_diffusers

        try:
            # By passing the specified VAE to the conversion function, the autoencoder
            # will be built into the model rather than tacked on afterward via the config file
            vae_model = None
            if vae:
                vae_model = self._load_vae(vae)
                vae_path = None
            convert_ckpt_to_diffusers(
                ckpt_path,
                diffusers_path,
                extract_ema=True,
                original_config_file=original_config_file,
                vae=vae_model,
                vae_path=vae_path,
                scan_needed=scan_needed,
            )
            self.logger.debug(
                f"Success. Converted model is now located at {str(diffusers_path)}"
            )
            self.logger.debug(f"Writing new config file entry for {model_name}")
            new_config = dict(
                path=str(diffusers_path),
                description=model_description,
                format="diffusers",
            )
            if model_name in self.config:
                self.del_model(model_name)
            self.add_model(model_name, new_config, True)
            if commit_to_conf:
                self.commit(commit_to_conf)
            self.logger.debug("Conversion succeeded")
        except Exception as e:
            self.logger.warning(f"Conversion failed: {str(e)}")
            self.logger.warning(
                "If you are trying to convert an inpainting or 2.X model, please indicate the correct config file (e.g. v1-inpainting-inference.yaml)"
            )

        return model_name

    def search_models(self, search_folder):
        self.logger.info(f"Finding Models In: {search_folder}")
        models_folder_ckpt = Path(search_folder).glob("**/*.ckpt")
        models_folder_safetensors = Path(search_folder).glob("**/*.safetensors")

        ckpt_files = [x for x in models_folder_ckpt if x.is_file()]
        safetensor_files = [x for x in models_folder_safetensors if x.is_file()]

        files = ckpt_files + safetensor_files

        found_models = []
        for file in files:
            location = str(file.resolve()).replace("\\", "/")
            if (
                "model.safetensors" not in location
                and "diffusion_pytorch_model.safetensors" not in location
            ):
                found_models.append({"name": file.stem, "location": location})

        return search_folder, found_models

    def commit(self) -> None:
        """
        Write current configuration out to the indicated file.
        """
        yaml_str = OmegaConf.to_yaml(self.config)
        config_file_path = self.config_path
        tmpfile = os.path.join(os.path.dirname(config_file_path), "new_config.tmp")
        with open(tmpfile, "w", encoding="utf-8") as outfile:
            outfile.write(self.preamble())
            outfile.write(yaml_str)
        os.replace(tmpfile, config_file_path)

    def preamble(self) -> str:
        """
        Returns the preamble for the config file.
        """
        return textwrap.dedent(
            """\
            # This file describes the alternative machine learning models
            # available to InvokeAI script.
            #
            # To add a new model, follow the examples below. Each
            # model requires a model config file, a weights file,
            # and the width and height of the images it
            # was trained on.
        """
        )

    @classmethod
    def _delete_model_from_cache(cls,repo_id):
        cache_info = scan_cache_dir(global_cache_dir("hub"))

        # I'm sure there is a way to do this with comprehensions
        # but the code quickly became incomprehensible!
        hashes_to_delete = set()
        for repo in cache_info.repos:
            if repo.repo_id == repo_id:
                for revision in repo.revisions:
                    hashes_to_delete.add(revision.commit_hash)
        strategy = cache_info.delete_revisions(*hashes_to_delete)
        cls.logger.warning(
            f"Deletion of this model is expected to free {strategy.expected_freed_size_str}"
        )
        strategy.execute()

    @staticmethod
    def _abs_path(path: str | Path) -> Path:
        if path is None or Path(path).is_absolute():
            return path
        return Path(Globals.root, path).resolve()

